"use client";

import { useState, useTransition } from "react";

import { loginAction } from "@/app/admin/actions";

export function LoginForm() {
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function submit(formData: FormData) {
    setError(null);
    startTransition(async () => {
      const state = await loginAction(formData);
      // On success the page re-renders as the admin view, so there is nothing to
      // do here but report failure.
      if (state !== "ok") setError("Incorrect password.");
    });
  }

  return (
    <main className="mx-auto flex w-full max-w-sm flex-col gap-4 px-5 py-24">
      <h1 className="text-xl font-bold">Admin sign in</h1>
      <form action={submit} className="flex flex-col gap-3">
        <input
          name="password"
          type="password"
          autoFocus
          required
          placeholder="Admin password"
          className="rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
        />
        <button
          type="submit"
          disabled={pending}
          className="rounded-md bg-blue-600 px-3 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-40"
        >
          {pending ? "Checking…" : "Sign in"}
        </button>
      </form>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <p className="text-xs text-neutral-500">
        Set with <code>ADMIN_PASSWORD</code> in the environment.
      </p>
    </main>
  );
}
