import { Client } from "@gradio/client";
import { z } from "zod";

/**
 * The ONLY file that knows the inference service is a Gradio Space.
 *
 * Everything else calls `analyzeImage()` and gets typed data back. If we ever
 * move to FastAPI, a different host, or a paid GPU, this file changes and
 * nothing else does.
 *
 * Why @gradio/client instead of fetch: Gradio's HTTP API is a three-step
 * protocol (upload file, POST for an event_id, then parse a Server-Sent Events
 * stream). The official client hides that. We send the image as base64 rather
 * than a file upload, which skips the upload step entirely — one round trip.
 */

const rawVerdictSchema = z.object({
  tag: z.string(),
  // The Space applies its own provisional thresholds, but we ignore its verdict
  // and re-decide from `score` using thresholds in Postgres. See
  // lib/tags/decide.ts for why.
  present: z.boolean().nullable(),
  score: z.number(),
  sigmoid: z.number(),
  band: z.enum(["present", "absent", "uncertain"]),
  confidence: z.string(),
  decided_by: z.string(),
  top_phrase: z.string(),
  crop: z.array(z.number()).length(4),
});

const rawAnalysisSchema = z.object({
  model: z.string(),
  packs_version: z.string(),
  latency_ms: z.number(),
  image_size: z.array(z.number()).length(2),
  results: z.array(rawVerdictSchema),
});

export type RawVerdict = z.infer<typeof rawVerdictSchema>;
export type RawAnalysis = z.infer<typeof rawAnalysisSchema>;

/** The Space is asleep or still loading the model. Callers should surface a
 * retryable 503, not a generic failure. */
export class InferenceWarmingError extends Error {
  readonly retryAfter: number;
  constructor(message: string, retryAfter = 30) {
    super(message);
    this.name = "InferenceWarmingError";
    this.retryAfter = retryAfter;
  }
}

export class InferenceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InferenceError";
  }
}

/**
 * Warm requests take ~600ms-2s. A cold Space has to download and load ~400MB of
 * weights, which blows well past Vercel's 10s function ceiling — so we give up
 * early and report "warming" rather than hanging until the platform kills us
 * mid-request with no useful error.
 */
const CALL_TIMEOUT_MS = 8_000;
const CONNECT_TIMEOUT_MS = 6_000;

function target(): string {
  const space = process.env.HF_SPACE?.trim();
  const local = process.env.INFERENCE_URL?.trim();

  // A configured Space wins; INFERENCE_URL is the local-dev escape hatch that
  // lets you run `python app.py` and develop without deploying.
  if (space) return space;
  if (local) return local;

  throw new InferenceError(
    "Neither HF_SPACE nor INFERENCE_URL is set — cannot reach the inference service",
  );
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new InferenceWarmingError(`${label} exceeded ${ms}ms`)),
      ms,
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

/**
 * Cached connection. Module scope survives across requests on a warm serverless
 * instance, so we pay the Gradio handshake once rather than per call.
 *
 * The promise itself is cached (not just the resolved client) so concurrent
 * requests during a cold start share one handshake instead of stampeding.
 */
let clientPromise: Promise<Client> | null = null;

function connect(): Promise<Client> {
  if (!clientPromise) {
    const token = process.env.HF_TOKEN?.trim();
    clientPromise = withTimeout(
      Client.connect(target(), {
        // Required for a private Space. Omitted entirely when absent, because
        // passing an empty token is treated as a malformed credential.
        ...(token ? { hf_token: token as `hf_${string}` } : {}),
      }),
      CONNECT_TIMEOUT_MS,
      "connect",
    ).catch((error) => {
      // Never cache a failed handshake, or one blip poisons the instance until
      // it is recycled.
      clientPromise = null;
      throw error;
    });
  }
  return clientPromise;
}

async function callEndpoint(name: string, payload: unknown[]): Promise<unknown> {
  let client: Client;
  try {
    client = await connect();
  } catch (error) {
    if (error instanceof InferenceWarmingError) throw error;
    throw new InferenceWarmingError(
      `could not reach inference service: ${(error as Error).message}`,
    );
  }

  try {
    const result = await withTimeout(
      client.predict(`/${name}`, payload),
      CALL_TIMEOUT_MS,
      `/${name}`,
    );
    // Gradio wraps outputs in a `data` array, one entry per declared output.
    // Our endpoints declare exactly one.
    return (result as { data: unknown[] }).data?.[0];
  } catch (error) {
    if (error instanceof InferenceWarmingError) throw error;
    // A dropped connection usually means the Space restarted; force a fresh
    // handshake next time.
    clientPromise = null;
    throw new InferenceError((error as Error).message || "inference call failed");
  }
}

/**
 * Score an image against tags.
 *
 * @param imageBase64 raw base64 (no data: prefix needed, both accepted)
 * @param tags known tag slugs; unknown slugs are rejected by the Space
 */
export async function analyzeImage(
  imageBase64: string,
  tags: string[],
): Promise<RawAnalysis> {
  const raw = await callEndpoint("analyze", [imageBase64, tags]);
  const parsed = rawAnalysisSchema.safeParse(raw);

  if (!parsed.success) {
    // The Space returned something we don't understand — a version skew or a
    // Gradio error object. Fail loudly rather than guessing at the shape.
    throw new InferenceError(
      `unexpected response from inference service: ${parsed.error.message}`,
    );
  }
  return parsed.data;
}

const healthSchema = z.object({
  status: z.string(),
  model: z.string(),
  packs_version: z.string(),
  tags: z.array(z.string()),
  prompts: z.number(),
});

export type InferenceHealth = z.infer<typeof healthSchema>;

export async function inferenceHealth(): Promise<InferenceHealth> {
  const raw = await callEndpoint("health", []);
  return healthSchema.parse(raw);
}

/**
 * Memoised health, used on the /analyze hot path.
 *
 * /analyze needs `packs_version` (it is part of the cache key) and the tag list
 * (to reject unknown tags) before it can do anything. Fetching both from the
 * Space on every request cost a full network round trip even on a cache hit —
 * measured at ~600ms for a request that should be a single database read.
 *
 * The tag list and pack fingerprint only change when the Space is redeployed, so
 * a short TTL is safe. The window means that immediately after a deploy we may
 * serve one minute of scores keyed to the previous pack version; acceptable,
 * given the alternative is paying a round trip forever.
 */
const HEALTH_TTL_MS = 60_000;
let healthCache: { at: number; value: InferenceHealth } | null = null;

export async function cachedInferenceHealth(): Promise<InferenceHealth> {
  if (healthCache && Date.now() - healthCache.at < HEALTH_TTL_MS) {
    return healthCache.value;
  }
  const value = await inferenceHealth();
  healthCache = { at: Date.now(), value };
  return value;
}

const catalogSchema = z.object({
  packs_version: z.string(),
  tags: z.array(
    z.object({ slug: z.string(), label: z.string(), description: z.string() }),
  ),
});

export type TagCatalog = z.infer<typeof catalogSchema>;

export async function tagCatalog(): Promise<TagCatalog> {
  const raw = await callEndpoint("tags", []);
  return catalogSchema.parse(raw);
}
