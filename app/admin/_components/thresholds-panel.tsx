"use client";

import { useState, useTransition } from "react";

import { saveThresholdAction } from "@/app/admin/actions";

export type ThresholdRow = {
  slug: string;
  thresholdLow: number;
  thresholdHigh: number;
  sigmoidFloor: number;
  calibrated: boolean;
  precision: number | null;
  recall: number | null;
  updatedBy: string | null;
  updatedAt: Date;
};

/**
 * Threshold editor.
 *
 * Edits take effect on the very next request — no Space redeploy, no model
 * reload — because the API tier applies thresholds after inference returns a raw
 * score. That is the whole reason these live in Postgres while the prompt packs
 * live in git.
 */
export function ThresholdsPanel({ rows }: { rows: ThresholdRow[] }) {
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  function save(formData: FormData) {
    setMessage(null);
    startTransition(async () => {
      const result = await saveThresholdAction(formData);
      setMessage({ ok: result.ok, text: result.message ?? "Saved." });
    });
  }

  const calibratedCount = rows.filter((r) => r.calibrated).length;

  return (
    <section className="flex flex-col gap-3">
      <div>
        <h2 className="text-lg font-semibold">Decision thresholds</h2>
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
          A score at or above <strong>high</strong> is <em>present</em>; at or below{" "}
          <strong>low</strong> is <em>absent</em>; in between is <em>uncertain</em>.
          The <strong>floor</strong> is a veto — if nothing in the image resembles the
          tag in absolute terms, the verdict is absent regardless of score. Keep it
          low: measured true positives run as low as 0.028.
        </p>
      </div>

      {calibratedCount < rows.length && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          <strong>
            {rows.length - calibratedCount} of {rows.length} tags are uncalibrated.
          </strong>{" "}
          Their thresholds are educated guesses. Run{" "}
          <code>inference/calibrate.py</code> against labelled images to replace them
          with measured values.
        </div>
      )}

      {message && (
        <p
          className={`text-sm ${
            message.ok
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-red-600 dark:text-red-400"
          }`}
        >
          {message.text}
        </p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full min-w-3xl text-sm">
          <thead className="text-left text-xs uppercase text-neutral-500">
            <tr>
              <th className="py-2 pr-3 font-medium">Tag</th>
              <th className="py-2 pr-3 font-medium">Low</th>
              <th className="py-2 pr-3 font-medium">High</th>
              <th className="py-2 pr-3 font-medium">Floor</th>
              <th className="py-2 pr-3 font-medium">Calibration</th>
              <th className="py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-200 dark:divide-neutral-800">
            {rows.map((row) => (
              <tr key={row.slug}>
                <td className="py-1.5 pr-3 font-mono text-xs">{row.slug}</td>
                <td colSpan={5} className="py-1.5">
                  <form action={save} className="flex flex-wrap items-center gap-2">
                    <input type="hidden" name="slug" value={row.slug} />
                    <input
                      name="low"
                      type="number"
                      step="0.005"
                      min="0"
                      max="1"
                      defaultValue={row.thresholdLow}
                      className="w-20 rounded border border-neutral-300 bg-transparent px-1.5 py-1 text-xs tabular-nums dark:border-neutral-700"
                    />
                    <input
                      name="high"
                      type="number"
                      step="0.005"
                      min="0"
                      max="1"
                      defaultValue={row.thresholdHigh}
                      className="w-20 rounded border border-neutral-300 bg-transparent px-1.5 py-1 text-xs tabular-nums dark:border-neutral-700"
                    />
                    <input
                      name="floor"
                      type="number"
                      step="0.001"
                      min="0"
                      max="1"
                      defaultValue={row.sigmoidFloor}
                      className="w-20 rounded border border-neutral-300 bg-transparent px-1.5 py-1 text-xs tabular-nums dark:border-neutral-700"
                    />
                    <span className="min-w-40 text-xs text-neutral-500">
                      {row.calibrated ? (
                        <span className="text-emerald-600 dark:text-emerald-400">
                          calibrated
                          {row.precision !== null &&
                            ` · P=${(row.precision * 100).toFixed(0)}%`}
                          {row.recall !== null && ` R=${(row.recall * 100).toFixed(0)}%`}
                        </span>
                      ) : (
                        <span className="text-amber-600 dark:text-amber-400">
                          uncalibrated (guess)
                        </span>
                      )}
                    </span>
                    <button
                      type="submit"
                      disabled={pending}
                      className="rounded border border-neutral-300 px-2 py-1 text-xs hover:bg-neutral-100 disabled:opacity-40 dark:border-neutral-700 dark:hover:bg-neutral-800"
                    >
                      Save
                    </button>
                  </form>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-neutral-500">
        Saving marks a tag <em>uncalibrated</em> — a hand-edited threshold is no longer
        the measured one, and the recorded precision would no longer describe it.
      </p>
    </section>
  );
}
