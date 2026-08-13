import dns from "node:dns/promises";
import net from "node:net";

/**
 * Turning a request into image bytes + a hash.
 *
 * Accepts three shapes so callers can use whichever fits:
 *   - multipart/form-data with `image` (a file) and repeated `tags` fields
 *   - JSON { image_base64, tags[] }
 *   - JSON { image_url, tags[] }
 */

// Vercel serverless caps request bodies at roughly 4.5MB. We reject a little
// under that so the failure is our clear error rather than the platform's
// opaque one. The browser client downscales to 768px (~150KB), so anything
// approaching this limit is a misbehaving caller, not a normal upload.
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

export class ImageIntakeError extends Error {
  readonly code:
    | "NO_IMAGE"
    | "NO_TAGS"
    | "IMAGE_TOO_LARGE"
    | "INVALID_IMAGE"
    | "INVALID_IMAGE_URL"
    | "BAD_REQUEST";
  constructor(code: ImageIntakeError["code"], message: string) {
    super(message);
    this.code = code;
  }
}

export type Intake = {
  base64: string;
  /** sha256 of the raw bytes — the cache key and the audit record. The image
   * itself is never stored. */
  hash: string;
  bytes: number;
  tags: string[];
};

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as unknown as ArrayBuffer);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function toBase64(bytes: Uint8Array): string {
  // Chunked to avoid blowing the argument limit on String.fromCharCode for
  // multi-megabyte images.
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/**
 * Reject URLs that point back into our own infrastructure.
 *
 * This endpoint fetches a URL on the caller's behalf, which without checks is a
 * server-side request forgery primitive: someone could pass
 * http://169.254.169.254/ to read cloud instance metadata, or a private address
 * to probe internal services. We resolve the hostname and refuse anything that
 * is not a public IP.
 */
async function assertPublicUrl(raw: string): Promise<URL> {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new ImageIntakeError("INVALID_IMAGE_URL", `Not a valid URL: ${raw}`);
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new ImageIntakeError(
      "INVALID_IMAGE_URL",
      `Only http and https URLs are supported, got ${url.protocol}`,
    );
  }

  let addresses: { address: string }[];
  try {
    addresses = await dns.lookup(url.hostname, { all: true });
  } catch {
    throw new ImageIntakeError(
      "INVALID_IMAGE_URL",
      `Could not resolve host ${url.hostname}`,
    );
  }

  for (const { address } of addresses) {
    if (isPrivateAddress(address)) {
      throw new ImageIntakeError(
        "INVALID_IMAGE_URL",
        `Refusing to fetch a private or loopback address (${url.hostname})`,
      );
    }
  }
  return url;
}

function isPrivateAddress(address: string): boolean {
  if (net.isIPv4(address)) {
    const [a, b] = address.split(".").map(Number);
    return (
      a === 0 || // this network
      a === 10 || // private
      a === 127 || // loopback
      (a === 169 && b === 254) || // link-local, incl. cloud metadata
      (a === 172 && b >= 16 && b <= 31) || // private
      (a === 192 && b === 168) || // private
      (a === 100 && b >= 64 && b <= 127) || // carrier NAT
      a >= 224 // multicast / reserved
    );
  }
  if (net.isIPv6(address)) {
    const ip = address.toLowerCase();
    return (
      ip === "::1" ||
      ip === "::" ||
      ip.startsWith("fc") || // unique local
      ip.startsWith("fd") ||
      ip.startsWith("fe80") || // link-local
      ip.startsWith("::ffff:") // IPv4-mapped — re-check as v4
    );
  }
  return true; // unrecognised form: refuse
}

function normaliseTags(input: unknown): string[] {
  const list = Array.isArray(input)
    ? input
    : typeof input === "string"
      ? input.split(",")
      : [];

  const tags = [...new Set(list.map((t) => String(t).trim()).filter(Boolean))];
  if (tags.length === 0) {
    throw new ImageIntakeError("NO_TAGS", "Provide at least one tag to verify.");
  }
  return tags;
}

async function finish(bytes: Uint8Array, tags: string[]): Promise<Intake> {
  if (bytes.length === 0) {
    throw new ImageIntakeError("INVALID_IMAGE", "The image was empty.");
  }
  if (bytes.length > MAX_IMAGE_BYTES) {
    throw new ImageIntakeError(
      "IMAGE_TOO_LARGE",
      `Image is ${Math.round(bytes.length / 1024)}KB; the limit is ${
        MAX_IMAGE_BYTES / 1024 / 1024
      }MB. Downscale before uploading — 768px on the longest edge is plenty.`,
    );
  }
  return {
    base64: toBase64(bytes),
    hash: await sha256Hex(bytes),
    bytes: bytes.length,
    tags,
  };
}

export async function readIntake(request: Request): Promise<Intake> {
  const contentType = request.headers.get("content-type") ?? "";

  if (contentType.includes("multipart/form-data")) {
    const form = await request.formData();
    const file = form.get("image");
    if (!(file instanceof File)) {
      throw new ImageIntakeError("NO_IMAGE", "Attach the image as the `image` field.");
    }
    // Support both repeated `tags` fields and one comma-separated value.
    const many = form.getAll("tags").map(String);
    const tags = normaliseTags(many.length > 1 ? many : (many[0] ?? ""));
    return finish(new Uint8Array(await file.arrayBuffer()), tags);
  }

  if (contentType.includes("application/json")) {
    let body: Record<string, unknown>;
    try {
      body = await request.json();
    } catch {
      throw new ImageIntakeError("BAD_REQUEST", "Body is not valid JSON.");
    }

    const tags = normaliseTags(body.tags);

    if (typeof body.image_base64 === "string" && body.image_base64.trim()) {
      const payload = body.image_base64.includes(",")
        ? body.image_base64.slice(body.image_base64.indexOf(",") + 1)
        : body.image_base64;
      try {
        const binary = atob(payload.trim());
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        return finish(bytes, tags);
      } catch {
        throw new ImageIntakeError("INVALID_IMAGE", "image_base64 is not valid base64.");
      }
    }

    if (typeof body.image_url === "string" && body.image_url.trim()) {
      const url = await assertPublicUrl(body.image_url.trim());
      let response: Response;
      try {
        response = await fetch(url, {
          redirect: "error", // a redirect could hop to a private address
          signal: AbortSignal.timeout(10_000),
        });
      } catch (error) {
        throw new ImageIntakeError(
          "INVALID_IMAGE_URL",
          `Could not fetch image_url: ${(error as Error).message}`,
        );
      }
      if (!response.ok) {
        throw new ImageIntakeError(
          "INVALID_IMAGE_URL",
          `image_url returned HTTP ${response.status}`,
        );
      }
      return finish(new Uint8Array(await response.arrayBuffer()), tags);
    }

    throw new ImageIntakeError(
      "NO_IMAGE",
      "Provide `image_base64` or `image_url`, or POST multipart/form-data.",
    );
  }

  throw new ImageIntakeError(
    "BAD_REQUEST",
    "Use multipart/form-data or application/json.",
  );
}
