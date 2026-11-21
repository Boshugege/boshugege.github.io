// Proof of work for the 观测 button (see ops/reactions-api and SERVER.md 9).
// The server hands out a signed, short-lived challenge; the browser finds a
// nonce such that SHA-256(challenge + ":" + nonce) starts with `difficulty`
// zero bits. Invisible to a reader (tens of milliseconds), but it makes every
// scripted reaction cost CPU. Runs in browsers and in Node (Web Crypto).

/** Number of leading zero bits of a digest. */
export function leadingZeroBits(bytes: Uint8Array) {
  let bits = 0;
  for (const byte of bytes) {
    if (byte !== 0) return bits + Math.clz32(byte) - 24;
    bits += 8;
  }
  return bits;
}

export interface SolveOptions {
  /** Digests requested at once; Web Crypto runs them in parallel. */
  batchSize?: number;
  /** Give up after this many nonces (default: 256 times the expected work). */
  maxAttempts?: number;
  signal?: AbortSignal;
}

const encoder = new TextEncoder();

async function digest(challenge: string, nonce: string) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(`${challenge}:${nonce}`)));
}

/** Returns the smallest decimal nonce that meets the difficulty. */
export async function solvePow(challenge: string, difficulty: number, options: SolveOptions = {}) {
  if (!Number.isInteger(difficulty) || difficulty < 0 || difficulty > 32) throw new RangeError(`bad difficulty: ${difficulty}`);
  const batchSize = options.batchSize ?? 256;
  const maxAttempts = options.maxAttempts ?? 2 ** Math.min(difficulty + 8, 40);
  for (let start = 0; start < maxAttempts; start += batchSize) {
    options.signal?.throwIfAborted();
    const nonces = Array.from({ length: Math.min(batchSize, maxAttempts - start) }, (_, index) => String(start + index));
    const digests = await Promise.all(nonces.map((nonce) => digest(challenge, nonce)));
    const hit = digests.findIndex((bytes) => leadingZeroBits(bytes) >= difficulty);
    if (hit >= 0) return nonces[hit];
  }
  throw new Error(`no proof of work within ${maxAttempts} attempts`);
}

/** Checks a nonce the same way the server does. */
export async function checkPow(challenge: string, nonce: string, difficulty: number) {
  return /^(?:0|[1-9]\d{0,15})$/.test(nonce) && leadingZeroBits(await digest(challenge, nonce)) >= difficulty;
}
