import Link from "next/link";

export const metadata = { title: "API docs · Tag Verification" };

function Code({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-lg bg-neutral-900 p-4 text-xs leading-relaxed text-neutral-100">
      <code>{children}</code>
    </pre>
  );
}

export default function DocsPage() {
  return (
    <main className="mx-auto w-full max-w-3xl px-5 py-8">
      <header className="mb-8 flex items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">API</h1>
        <Link href="/" className="text-sm text-blue-600 hover:underline dark:text-blue-400">
          Playground
        </Link>
      </header>

      <div className="flex flex-col gap-8 text-sm leading-relaxed">
        <section>
          <h2 className="mb-2 text-lg font-semibold">Authentication</h2>
          <p className="text-neutral-600 dark:text-neutral-400">
            Send your key in the <code>x-api-key</code> header (
            <code>Authorization: Bearer …</code> also works). Keys are issued in{" "}
            <Link href="/admin" className="text-blue-600 hover:underline dark:text-blue-400">
              Admin
            </Link>{" "}
            and shown once — only a hash is stored.
          </p>
        </section>

        <section>
          <h2 className="mb-2 text-lg font-semibold">POST /api/v1/analyze</h2>
          <p className="mb-3 text-neutral-600 dark:text-neutral-400">
            Multipart, or JSON with <code>image_base64</code> or <code>image_url</code>.
            Downscale to 768px on the longest edge before sending — the model works at
            224px, so nothing is lost and requests stay small.
          </p>
          <Code>{`curl -X POST https://your-app/api/v1/analyze \\
  -H "x-api-key: dooh_live_..." \\
  -F "image=@creative.jpg" \\
  -F "tags=alcohol" -F "tags=gym_fitness"`}</Code>

          <p className="mt-4 mb-2 font-medium">Response</p>
          <Code>{`{
  "request_id": "5f1c...",
  "image_hash": "9a3f...",          // sha256; the image itself is never stored
  "model": "google/siglip2-base-patch16-224",
  "packs_version": "1a2db18ba940",
  "cached": false,
  "latency_ms": 612,
  "results": [{
    "tag": "alcohol",
    "present": true,                // true | false | null
    "score": 0.977,
    "confidence": "high",
    "decided_by": "siglip",
    "calibrated": false,
    "evidence": {
      "top_phrase": "a glass of beer with foam",
      "crop": [0.0, 0.5, 0.5, 1.0], // where it was found, as 0-1 fractions
      "sigmoid": 0.028
    }
  }],
  "uncalibrated_tags": ["alcohol"]
}`}</Code>
        </section>

        <section className="rounded-lg border border-amber-300 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950/40">
          <h2 className="mb-1 font-semibold text-amber-900 dark:text-amber-200">
            Two fields you must not ignore
          </h2>
          <ul className="ml-4 list-disc space-y-1.5 text-amber-800 dark:text-amber-300">
            <li>
              <code>present: null</code> means <strong>uncertain</strong>, not false. The
              score fell between the thresholds. Treat it as &ldquo;needs a human&rdquo;
              — reading it as absent is how a non-compliant creative slips through.
            </li>
            <li>
              <code>calibrated: false</code> means that tag&rsquo;s thresholds were never
              measured against labelled images. The verdict is an educated guess.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="mb-2 text-lg font-semibold">Other endpoints</h2>
          <ul className="ml-4 list-disc space-y-1 text-neutral-600 dark:text-neutral-400">
            <li>
              <code>GET /api/v1/tags</code> — the catalog, with a{" "}
              <code>calibrated</code> flag per tag
            </li>
            <li>
              <code>GET /api/v1/usage</code> — this key&rsquo;s traffic today
            </li>
            <li>
              <code>GET /api/v1/health</code> — database and inference status. No key
              required, so it can be used for monitoring.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="mb-2 text-lg font-semibold">Errors</h2>
          <p className="mb-3 text-neutral-600 dark:text-neutral-400">
            Every failure returns <code>{`{ "error": "CODE", "message": "..." }`}</code>{" "}
            so you can branch on the code.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left uppercase text-neutral-500">
                <tr>
                  <th className="py-1.5 pr-3 font-medium">Code</th>
                  <th className="py-1.5 pr-3 font-medium">Status</th>
                  <th className="py-1.5 font-medium">Meaning</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-200 dark:divide-neutral-800">
                {[
                  ["MISSING_KEY / INVALID_KEY", "401", "No key, or it is unknown or revoked"],
                  ["RATE_LIMITED", "429", "Over your per-minute limit; see Retry-After"],
                  ["UNKNOWN_TAG", "400", "A tag slug was not recognised — never treated as 'absent'"],
                  ["NO_TAGS / NO_IMAGE", "400", "Required input missing"],
                  ["IMAGE_TOO_LARGE", "413", "Over 4MB — downscale first"],
                  ["INVALID_IMAGE_URL", "400", "Unfetchable, or a private/loopback address"],
                  ["INFERENCE_WARMING", "503", "Service waking up; retry after Retry-After"],
                  ["INFERENCE_FAILED", "502", "Inference errored"],
                ].map(([code, status, meaning]) => (
                  <tr key={code}>
                    <td className="py-1.5 pr-3 font-mono">{code}</td>
                    <td className="py-1.5 pr-3 tabular-nums">{status}</td>
                    <td className="py-1.5 text-neutral-600 dark:text-neutral-400">
                      {meaning}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-neutral-600 dark:text-neutral-400">
            <code>UNKNOWN_TAG</code> is an error by design. If you misspell a tag we
            refuse the request and echo the valid list, because &ldquo;we didn&rsquo;t
            check&rdquo; and &ldquo;we checked and it is clean&rdquo; must never look
            alike.
          </p>
        </section>
      </div>
    </main>
  );
}
