import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { checkPow, leadingZeroBits, solvePow } from "../src/lib/pow.ts";
// The server's checker: the browser's nonce has to pass it byte for byte.
import { verifyPow } from "../ops/reactions-api/lib.mjs";

test("counts leading zero bits across byte boundaries", () => {
  assert.equal(leadingZeroBits(new Uint8Array([0b10000000])), 0);
  assert.equal(leadingZeroBits(new Uint8Array([0b00000001, 0xff])), 7);
  assert.equal(leadingZeroBits(new Uint8Array([0, 0b00100000])), 10);
  assert.equal(leadingZeroBits(new Uint8Array([0, 0, 0])), 24);
  assert.equal(leadingZeroBits(new Uint8Array([])), 0);
});

test("solves a challenge with the smallest qualifying nonce, accepted by the server's checker", async () => {
  const challenge = "v1.eyJ0IjoiL3Bvc3RzL2FyY2gtbGludXguaHRtbCJ9.c2lnbmF0dXJl";
  const difficulty = 12;
  const nonce = await solvePow(challenge, difficulty);
  assert.match(nonce, /^(?:0|[1-9]\d*)$/);
  assert.equal(await checkPow(challenge, nonce, difficulty), true);
  assert.equal(verifyPow(challenge, nonce, difficulty), true);
  // Independent check with node:crypto, and minimality.
  const bits = (value: string) => leadingZeroBits(createHash("sha256").update(`${challenge}:${value}`).digest());
  assert.ok(bits(nonce) >= difficulty);
  for (let candidate = 0; candidate < Number(nonce); candidate++) assert.ok(bits(String(candidate)) < difficulty);
});

test("rejects nonces that do not do the work or are not plain decimals", async () => {
  const challenge = "v1.payload.sig";
  const nonce = await solvePow(challenge, 8);
  const tooHard = leadingZeroBits(createHash("sha256").update(`${challenge}:${nonce}`).digest()) + 1;
  assert.equal(await checkPow(challenge, nonce, tooHard), false);
  for (const bad of ["", "-1", "01", "1.5", "1e3", " 1", "12345678901234567"]) {
    assert.equal(await checkPow(challenge, bad, 0), false, bad);
    assert.equal(verifyPow(challenge, bad, 0), false, bad);
  }
});

test("gives up after the attempt budget and honours abort", async () => {
  await assert.rejects(solvePow("v1.never", 32, { maxAttempts: 512 }), /no proof of work/);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(solvePow("v1.x", 20, { signal: controller.signal }), { name: "AbortError" });
  await assert.rejects(solvePow("v1.x", 40), RangeError);
});

test("difficulty 14 stays cheap enough to be invisible", async () => {
  // Expected work is 2^14 digests; allow a slow CI machine plenty of room.
  const started = performance.now();
  for (const challenge of ["v1.a.b", "v1.c.d", "v1.e.f"]) await solvePow(challenge, 14);
  assert.ok((performance.now() - started) / 3 < 2000);
});
