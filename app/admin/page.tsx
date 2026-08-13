import Link from "next/link";
import { desc } from "drizzle-orm";

import { logoutAction } from "@/app/admin/actions";
import { KeysPanel } from "@/app/admin/_components/keys-panel";
import { ThresholdsPanel } from "@/app/admin/_components/thresholds-panel";
import { LoginForm } from "@/app/admin/_components/login-form";
import { adminState } from "@/lib/auth/admin";
import { db } from "@/lib/db";
import { apiKeys, tagThresholds } from "@/lib/db/schema";

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  const state = await adminState();

  if (state === "unconfigured") {
    return (
      <main className="mx-auto w-full max-w-xl px-5 py-16">
        <h1 className="text-xl font-bold">Admin is not configured</h1>
        <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
          Set <code className="rounded bg-neutral-100 px-1 dark:bg-neutral-800">ADMIN_PASSWORD</code>{" "}
          in <code>.env.local</code> (and in Vercel) then reload.
        </p>
        <p className="mt-3 text-sm text-neutral-600 dark:text-neutral-400">
          Access is refused rather than allowed while unset — these pages can mint
          API keys and change the thresholds that decide compliance verdicts, so
          defaulting to open would be the dangerous choice.
        </p>
      </main>
    );
  }

  if (state === "locked") {
    return <LoginForm />;
  }

  // Two independent reads — issued together, since each Neon HTTP query is its
  // own round trip.
  const [keys, thresholds] = await Promise.all([
    db.select().from(apiKeys).orderBy(desc(apiKeys.createdAt)),
    db.select().from(tagThresholds).orderBy(tagThresholds.slug),
  ]);

  return (
    <main className="mx-auto w-full max-w-5xl px-5 py-8">
      <header className="mb-8 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Admin</h1>
        <div className="flex items-center gap-4 text-sm">
          <Link href="/" className="text-blue-600 hover:underline dark:text-blue-400">
            Playground
          </Link>
          <form action={logoutAction}>
            <button type="submit" className="text-neutral-500 hover:underline">
              Sign out
            </button>
          </form>
        </div>
      </header>

      <div className="flex flex-col gap-12">
        <KeysPanel keys={keys} />
        <ThresholdsPanel rows={thresholds} />
      </div>
    </main>
  );
}
