import { neon } from "@neondatabase/serverless";
import { drizzle } from "drizzle-orm/neon-http";

import * as schema from "./schema";

/**
 * Neon over HTTP rather than a TCP pool.
 *
 * Vercel's serverless functions are short-lived and can scale to many parallel
 * instances; a traditional connection pool would exhaust Postgres connections.
 * The HTTP driver issues one stateless request per query, which is exactly the
 * right shape for this runtime — and it's what keeps us inside Neon's free tier
 * compute allowance, since nothing sits idle holding a connection open.
 *
 * Trade-off: no transactions across multiple statements over HTTP. Nothing in
 * this app needs them; if that changes, swap to `drizzle-orm/neon-serverless`
 * (WebSocket) for the paths that do.
 *
 * NOTE ON LATENCY: each query is a separate HTTPS request, so request time is
 * (queries x round-trip). Cross-region that is ~560-800ms *per query*. Keep the
 * number of sequential queries per request small, and deploy in the same region
 * as the database — see docs/DEPLOY.md.
 */

type Database = ReturnType<typeof drizzle<typeof schema>>;

let instance: Database | null = null;

function connect(): Database {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error(
      "DATABASE_URL is not set. Copy it from the Neon dashboard into .env.local",
    );
  }
  instance ??= drizzle(neon(url), { schema });
  return instance;
}

/**
 * Connection is created on FIRST USE, not at import.
 *
 * Eagerly connecting at module scope meant that importing anything from a module
 * that merely *mentions* the database required DATABASE_URL to be set — which
 * made pure functions like `decide()` untestable without a live connection, and
 * would fail a build step that only wanted a type. The proxy keeps the ergonomic
 * `db.select()...` call shape while deferring the work.
 */
export const db = new Proxy({} as Database, {
  get(_target, property, receiver) {
    return Reflect.get(connect(), property, receiver);
  },
});

export { schema };
