"use client";

import { useState, useTransition } from "react";

import { createKeyAction, revokeKeyAction } from "@/app/admin/actions";

export type KeyRow = {
  id: string;
  name: string;
  keyPrefix: string;
  rateLimitPerMin: number;
  revokedAt: Date | null;
  lastUsedAt: Date | null;
  createdAt: Date;
};

export function KeysPanel({ keys }: { keys: KeyRow[] }) {
  const [issued, setIssued] = useState<{ plaintext: string; name: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [copied, setCopied] = useState(false);

  function create(formData: FormData) {
    setError(null);
    startTransition(async () => {
      const result = await createKeyAction(formData);
      if (result.ok) setIssued({ plaintext: result.plaintext, name: result.name });
      else setError(result.message);
    });
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold">API keys</h2>

      {/* Shown once and never again — the database holds only a sha256 hash. */}
      {issued && (
        <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-4 dark:border-emerald-800 dark:bg-emerald-950/40">
          <p className="text-sm font-semibold text-emerald-900 dark:text-emerald-200">
            Key created for &ldquo;{issued.name}&rdquo; — copy it now
          </p>
          <p className="mt-1 text-xs text-emerald-800 dark:text-emerald-300">
            This is the only time it can be shown. Only a hash is stored, so it
            cannot be recovered — if it is lost, revoke it and issue another.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 break-all rounded bg-white px-2 py-1.5 font-mono text-xs dark:bg-neutral-900">
              {issued.plaintext}
            </code>
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard.writeText(issued.plaintext);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
              className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700"
            >
              {copied ? "Copied" : "Copy"}
            </button>
            <button
              type="button"
              onClick={() => setIssued(null)}
              className="text-xs text-emerald-800 underline dark:text-emerald-300"
            >
              Done
            </button>
          </div>
        </div>
      )}

      <form action={create} className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-neutral-500">Name</span>
          <input
            name="name"
            required
            placeholder="Client or project"
            className="w-56 rounded-md border border-neutral-300 bg-transparent px-2.5 py-1.5 text-sm dark:border-neutral-700"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-neutral-500">Requests / min</span>
          <input
            name="rateLimit"
            type="number"
            defaultValue={60}
            min={1}
            max={10000}
            className="w-28 rounded-md border border-neutral-300 bg-transparent px-2.5 py-1.5 text-sm dark:border-neutral-700"
          />
        </label>
        <button
          type="submit"
          disabled={pending}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-40"
        >
          {pending ? "Creating…" : "Create key"}
        </button>
      </form>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {keys.length === 0 ? (
        <p className="text-sm text-neutral-500">No keys yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-2xl text-sm">
            <thead className="text-left text-xs uppercase text-neutral-500">
              <tr>
                <th className="py-2 pr-3 font-medium">Name</th>
                <th className="py-2 pr-3 font-medium">Key</th>
                <th className="py-2 pr-3 font-medium">Limit</th>
                <th className="py-2 pr-3 font-medium">Last used</th>
                <th className="py-2 pr-3 font-medium">Status</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-200 dark:divide-neutral-800">
              {keys.map((key) => (
                <tr key={key.id} className={key.revokedAt ? "opacity-50" : undefined}>
                  <td className="py-2 pr-3">{key.name}</td>
                  <td className="py-2 pr-3 font-mono text-xs">
                    dooh_live_{key.keyPrefix}…
                  </td>
                  <td className="py-2 pr-3 tabular-nums">{key.rateLimitPerMin}/min</td>
                  <td className="py-2 pr-3 text-xs text-neutral-500">
                    {key.lastUsedAt
                      ? new Date(key.lastUsedAt).toLocaleString()
                      : "never"}
                  </td>
                  <td className="py-2 pr-3">
                    {key.revokedAt ? (
                      <span className="text-xs text-neutral-500">revoked</span>
                    ) : (
                      <span className="text-xs text-emerald-600 dark:text-emerald-400">
                        active
                      </span>
                    )}
                  </td>
                  <td className="py-2">
                    {!key.revokedAt && (
                      <button
                        type="button"
                        onClick={() => {
                          if (
                            confirm(
                              `Revoke "${key.name}"? Any integration using it will start getting 401s immediately.`,
                            )
                          ) {
                            startTransition(async () => {
                              await revokeKeyAction(key.id);
                            });
                          }
                        }}
                        className="text-xs text-red-600 hover:underline dark:text-red-400"
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
