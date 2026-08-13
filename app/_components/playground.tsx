"use client";

import { useCallback, useRef, useState, useTransition } from "react";

import { analyzeAction, type PlaygroundResult } from "@/app/actions";

export type TagOption = { slug: string; label: string; description: string; calibrated: boolean };

/**
 * Longest edge we upload. SigLIP consumes 224x224 tiles, so anything beyond this
 * is thrown away by the model anyway — downscaling in the browser costs zero
 * accuracy while removing the request-size limit and most of the upload time.
 * A 4K creative goes from ~8MB to ~150KB.
 */
const MAX_EDGE = 768;

type Prepared = { file: File; previewUrl: string; originalKb: number; uploadKb: number };

async function downscale(file: File): Promise<Prepared> {
  const bitmap = await createImageBitmap(file);
  // Never upscale — a small source stays as-is.
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const width = Math.round(bitmap.width * scale);
  const height = Math.round(bitmap.height * scale);

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas unavailable");
  ctx.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();

  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", 0.9),
  );
  if (!blob) throw new Error("could not encode image");

  return {
    file: new File([blob], "creative.jpg", { type: "image/jpeg" }),
    previewUrl: URL.createObjectURL(blob),
    originalKb: Math.round(file.size / 1024),
    uploadKb: Math.round(blob.size / 1024),
  };
}

export function Playground({ tags }: { tags: TagOption[] }) {
  const [prepared, setPrepared] = useState<Prepared | null>(null);
  const [selected, setSelected] = useState<string[]>(["alcohol"]);
  const [result, setResult] = useState<PlaygroundResult | null>(null);
  const [dragging, setDragging] = useState(false);
  const [pending, startTransition] = useTransition();
  const inputRef = useRef<HTMLInputElement>(null);

  const accept = useCallback(async (file: File | undefined) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setResult({ ok: false, message: `${file.name} is not an image.` });
      return;
    }
    setResult(null);
    try {
      setPrepared(await downscale(file));
    } catch (error) {
      setResult({ ok: false, message: `Could not read that image: ${(error as Error).message}` });
    }
  }, []);

  function submit() {
    if (!prepared || selected.length === 0) return;
    const data = new FormData();
    data.append("image", prepared.file);
    for (const tag of selected) data.append("tags", tag);
    startTransition(async () => setResult(await analyzeAction(data)));
  }

  const toggle = (slug: string) =>
    setSelected((prev) =>
      prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug],
    );

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
      {/* ---------------------------------------------------------- controls */}
      <div className="flex flex-col gap-4">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            void accept(e.dataTransfer.files[0]);
          }}
          onClick={() => inputRef.current?.click()}
          className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed p-6 text-center transition ${
            dragging
              ? "border-blue-500 bg-blue-500/10"
              : "border-neutral-300 hover:border-neutral-400 dark:border-neutral-700 dark:hover:border-neutral-600"
          }`}
        >
          <input
            ref={inputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => void accept(e.target.files?.[0])}
          />
          {prepared ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={prepared.previewUrl}
              alt="Selected creative"
              className="max-h-44 w-auto rounded-lg"
            />
          ) : (
            <>
              <span className="text-2xl">🖼️</span>
              <span className="text-sm font-medium">Drop a creative here</span>
              <span className="text-xs text-neutral-500">or click to choose a file</span>
            </>
          )}
        </div>

        {prepared && (
          <p className="text-xs text-neutral-500">
            Downscaled in your browser: {prepared.originalKb}KB → {prepared.uploadKb}KB
            {prepared.originalKb > prepared.uploadKb && " (no accuracy lost — the model sees 224px tiles)"}
          </p>
        )}

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-semibold">
            Tags to verify{" "}
            <span className="font-normal text-neutral-500">({selected.length} selected)</span>
          </legend>
          <div className="flex max-h-72 flex-wrap gap-1.5 overflow-y-auto rounded-lg border border-neutral-200 p-2 dark:border-neutral-800">
            {tags.map((tag) => {
              const on = selected.includes(tag.slug);
              return (
                <button
                  key={tag.slug}
                  type="button"
                  onClick={() => toggle(tag.slug)}
                  title={tag.description}
                  className={`rounded-full px-2.5 py-1 text-xs transition ${
                    on
                      ? "bg-blue-600 text-white"
                      : "bg-neutral-100 text-neutral-700 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-700"
                  }`}
                >
                  {tag.label}
                </button>
              );
            })}
          </div>
        </fieldset>

        <button
          type="button"
          onClick={submit}
          disabled={pending || !prepared || selected.length === 0}
          className="rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {pending ? "Analysing…" : "Analyse"}
        </button>
      </div>

      {/* ----------------------------------------------------------- results */}
      <div className="min-w-0">
        {!result && !pending && (
          <div className="rounded-xl border border-neutral-200 p-8 text-center text-sm text-neutral-500 dark:border-neutral-800">
            Results appear here. Try a creative that <em>nearly</em> matches a tag — a
            juice bottle against <code>alcohol</code> — since near-misses are what
            actually test a detector.
          </div>
        )}

        {pending && (
          <div className="animate-pulse rounded-xl border border-neutral-200 p-8 text-center text-sm text-neutral-500 dark:border-neutral-800">
            Scoring the full image plus 9 overlapping crops…
          </div>
        )}

        {result && !result.ok && (
          <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
            {result.message}
            {result.retryable && (
              <button
                type="button"
                onClick={submit}
                className="ml-2 underline hover:no-underline"
              >
                retry
              </button>
            )}
          </div>
        )}

        {result?.ok && prepared && (
          <Results result={result} previewUrl={prepared.previewUrl} />
        )}
      </div>
    </div>
  );
}

const BADGE: Record<string, { text: string; className: string }> = {
  present: { text: "PRESENT", className: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300" },
  absent: { text: "absent", className: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" },
  uncertain: { text: "UNCERTAIN", className: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300" },
};

function bandOf(present: boolean | null) {
  return present === null ? "uncertain" : present ? "present" : "absent";
}

function Results({
  result,
  previewUrl,
}: {
  result: Extract<PlaygroundResult, { ok: true }>;
  previewUrl: string;
}) {
  const [focused, setFocused] = useState<string | null>(null);
  const sorted = [...result.verdicts].sort((a, b) => b.score - a.score);
  const shown = sorted.find((v) => v.tag === focused) ?? sorted[0];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-neutral-500">
        <span>{result.latencyMs}ms</span>
        {result.cached && (
          <span className="rounded bg-neutral-100 px-1.5 py-0.5 dark:bg-neutral-800">
            served from cache
          </span>
        )}
        <span className="font-mono">{result.model}</span>
        <span className="font-mono">packs {result.packsVersion}</span>
      </div>

      {result.uncalibrated.length > 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          <strong>Uncalibrated:</strong> {result.uncalibrated.join(", ")}. These
          thresholds are educated guesses, not measurements — they become real once
          <code className="mx-1">calibrate.py</code> runs against labelled images.
        </div>
      )}

      {/* Evidence: show WHERE the winning crop was. A verdict nobody can see the
          basis for is a verdict nobody will trust. */}
      {shown && (
        <figure className="flex flex-col gap-1">
          <div className="relative inline-block max-w-md overflow-hidden rounded-lg border border-neutral-200 dark:border-neutral-800">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={previewUrl} alt="Analysed creative" className="block w-full" />
            <div
              className="pointer-events-none absolute border-2 border-blue-500 bg-blue-500/15"
              style={{
                left: `${shown.evidence.crop[0] * 100}%`,
                top: `${shown.evidence.crop[1] * 100}%`,
                width: `${(shown.evidence.crop[2] - shown.evidence.crop[0]) * 100}%`,
                height: `${(shown.evidence.crop[3] - shown.evidence.crop[1]) * 100}%`,
              }}
            />
          </div>
          <figcaption className="text-xs text-neutral-500">
            Best-scoring region for <strong>{shown.tag}</strong> — matched{" "}
            <em>&ldquo;{shown.evidence.topPhrase}&rdquo;</em>
          </figcaption>
        </figure>
      )}

      <div className="flex flex-col gap-2">
        {sorted.map((v) => {
          const band = bandOf(v.present);
          const badge = BADGE[band];
          const active = shown?.tag === v.tag;
          return (
            <button
              key={v.tag}
              type="button"
              onClick={() => setFocused(v.tag)}
              className={`rounded-lg border p-3 text-left transition ${
                active
                  ? "border-blue-500 bg-blue-50/50 dark:bg-blue-950/20"
                  : "border-neutral-200 hover:border-neutral-300 dark:border-neutral-800 dark:hover:border-neutral-700"
              }`}
            >
              <div className="flex items-center gap-2">
                <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${badge.className}`}>
                  {badge.text}
                </span>
                <span className="font-mono text-sm">{v.tag}</span>
                <span className="ml-auto font-mono text-sm tabular-nums">
                  {v.score.toFixed(3)}
                </span>
              </div>

              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800">
                <div
                  className={`h-full ${
                    band === "present"
                      ? "bg-red-500"
                      : band === "uncertain"
                        ? "bg-amber-500"
                        : "bg-emerald-500"
                  }`}
                  style={{ width: `${Math.max(1, v.score * 100)}%` }}
                />
              </div>

              <p className="mt-2 truncate text-xs text-neutral-500">
                {v.confidence} confidence · {v.decidedBy}
                {!v.calibrated && " · uncalibrated"} ·{" "}
                <em>&ldquo;{v.evidence.topPhrase}&rdquo;</em>
              </p>
            </button>
          );
        })}
      </div>
    </div>
  );
}
