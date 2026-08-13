import Link from "next/link";

import { Playground, type TagOption } from "@/app/_components/playground";
import { InferenceWarmingError, tagCatalog } from "@/lib/inference/client";
import { cachedThresholds } from "@/lib/tags/decide";

// The tag catalog comes from the Space, which can be asleep — so this page is
// always rendered per-request rather than cached at build time.
export const dynamic = "force-dynamic";

type CatalogState =
  | { ok: true; tags: TagOption[]; packsVersion: string }
  | { ok: false; warming: boolean; message: string };

async function loadCatalog(): Promise<CatalogState> {
  try {
    const [catalog, thresholds] = await Promise.all([tagCatalog(), cachedThresholds()]);
    return {
      ok: true,
      packsVersion: catalog.packs_version,
      tags: catalog.tags.map((tag) => ({
        ...tag,
        calibrated: thresholds.get(tag.slug)?.calibrated ?? false,
      })),
    };
  } catch (error) {
    return {
      ok: false,
      warming: error instanceof InferenceWarmingError,
      message: (error as Error).message,
    };
  }
}

export default async function Home() {
  const catalog = await loadCatalog();

  return (
    <main className="mx-auto w-full max-w-6xl px-5 py-8">
      <header className="mb-7 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Tag Verification</h1>
          <p className="mt-1 max-w-2xl text-sm text-neutral-600 dark:text-neutral-400">
            Check whether a creative actually contains the content it is tagged with.
            Each tag&rsquo;s prompts compete against hand-written{" "}
            <strong>hard negatives</strong> — whiskey has to beat <em>juice</em> — and
            the image is scored whole plus as 9 overlapping crops, so a small bottle in
            a corner still counts.
          </p>
        </div>
        <nav className="flex gap-3 text-sm">
          <Link href="/admin" className="text-blue-600 hover:underline dark:text-blue-400">
            Admin
          </Link>
          <Link href="/docs" className="text-blue-600 hover:underline dark:text-blue-400">
            API docs
          </Link>
        </nav>
      </header>

      {catalog.ok ? (
        <Playground tags={catalog.tags} />
      ) : (
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-5 text-sm dark:border-amber-800 dark:bg-amber-950/40">
          <p className="font-semibold text-amber-900 dark:text-amber-200">
            {catalog.warming
              ? "The inference service is starting up."
              : "Cannot reach the inference service."}
          </p>
          <p className="mt-1 text-amber-800 dark:text-amber-300">
            {catalog.warming
              ? "Free Spaces sleep after 48h idle and take ~30-60s to reload the model. Reload this page shortly."
              : catalog.message}
          </p>
          <p className="mt-3 text-xs text-amber-700 dark:text-amber-400">
            Running locally? Start it with{" "}
            <code className="rounded bg-amber-100 px-1 dark:bg-amber-900/50">
              cd inference &amp;&amp; ./.venv/bin/python app.py
            </code>
          </p>
        </div>
      )}
    </main>
  );
}
