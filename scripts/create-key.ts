/**
 * Issue an API key.
 *
 *   npm run create-key -- "Client name" [requests-per-minute]
 *
 * Prints the plaintext key ONCE. It is stored only as a sha256 hash, so it
 * cannot be recovered afterwards — if it is lost, revoke it and issue another.
 */
import { createApiKey } from "@/lib/auth/keys";

async function main() {
  const name = process.argv[2];
  const rateLimit = Number(process.argv[3] ?? 60);

  if (!name) {
    console.error('usage: npm run create-key -- "Client name" [requests-per-minute]');
    process.exit(1);
  }
  if (!Number.isFinite(rateLimit) || rateLimit < 1) {
    console.error(`invalid rate limit: ${process.argv[3]}`);
    process.exit(1);
  }

  const key = await createApiKey(name, rateLimit);

  console.log(`\n  name       ${name}`);
  console.log(`  rate limit ${rateLimit} requests/minute`);
  console.log(`\n  API KEY (shown once, store it now):\n\n    ${key.plaintext}\n`);
  console.log("  Try it:\n");
  console.log(
    `    curl -X POST http://localhost:3000/api/v1/analyze \\\n` +
      `      -H "x-api-key: ${key.plaintext}" \\\n` +
      `      -F "image=@creative.jpg" -F "tags=alcohol"\n`,
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
