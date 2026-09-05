import assert from "node:assert/strict";
import test from "node:test";
import { ALIGNMENT_HOLD, ASCII_CHARACTERS, asciiCellSize, createFragments, fallback404, HOVER_YAW, rotationAt, ROTATION_PERIOD, ROTATION_RAMP } from "../src/lib/impossible-404.ts";

test("404 fragments share front silhouettes but occupy different depths", () => {
  const pieces = createFragments();
  assert.equal(pieces.length, 20);
  assert.ok(new Set(pieces.map((piece) => piece.z)).size >= 8);
  assert.deepEqual(pieces.slice(0, 6).map((piece) => piece.outline), pieces.slice(-6).map((piece) => piece.outline));
  assert.notDeepEqual(pieces.slice(0, 6).map((piece) => piece.z), pieces.slice(-6).map((piece) => piece.z));
  for (const piece of pieces) {
    assert.ok(piece.thickness >= 0.55);
    assert.ok(piece.outline.length >= 3);
    for (const [x, y] of piece.outline) {
      assert.ok(Math.hypot(x + piece.x, y, piece.z + piece.thickness) < 5.8);
    }
  }
});

test("the four's diagonal, crossbar, and stem have consistent stroke weight", () => {
  const [diagonal, , crossbar, stem] = createFragments();
  const [a, b, , d] = diagonal.outline;
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const diagonalWidth = Math.abs(dx * (d[1] - a[1]) - dy * (d[0] - a[0])) / Math.hypot(dx, dy);
  const crossbarWidth = crossbar.outline[2][1] - crossbar.outline[1][1];
  const stemWidth = stem.outline[1][0] - stem.outline[0][0];
  assert.ok(Math.abs(crossbarWidth - stemWidth) < 1e-10);
  assert.ok(Math.abs(diagonalWidth - stemWidth) < 0.02);
});

test("the four's lowered crossbar stays joined to the diagonal and stem", () => {
  const [, diagonal, crossbar, upperStem, lowerStem] = createFragments();
  const top = crossbar.outline[2][1];
  const bottom = crossbar.outline[0][1];
  assert.ok(top < 0);
  assert.equal(diagonal.outline[0][1], top);
  assert.equal(diagonal.outline[3][1], top);
  assert.equal(upperStem.outline[0][1], top);
  assert.equal(lowerStem.outline[2][1], bottom);
});

test("the expanded ASCII palette preserves the original glyph sizes", () => {
  assert.ok(ASCII_CHARACTERS.length >= 64);
  assert.equal(new Set(ASCII_CHARACTERS).size, ASCII_CHARACTERS.length);
  assert.equal(ASCII_CHARACTERS[0], " ");
  assert.equal(asciiCellSize(320), 4.5);
  assert.equal(asciiCellSize(1100), 6);
});

test("front view holds, completes one turn, and returns seamlessly", () => {
  assert.deepEqual(rotationAt(0), { x: 0, y: 0, z: 0, aligned: true });
  assert.deepEqual(rotationAt(ALIGNMENT_HOLD - 0.01), rotationAt(0));
  assert.deepEqual(rotationAt(ROTATION_PERIOD), rotationAt(0));
  const middle = rotationAt((ROTATION_PERIOD + ALIGNMENT_HOLD) / 2);
  assert.ok(Math.abs(middle.y - Math.PI) < 1e-10);
  const end = rotationAt(ROTATION_PERIOD - 0.001);
  assert.ok(Math.abs(end.y - Math.PI * 2) < 1e-6);
  assert.ok(Math.abs(end.x) < 1e-6);
  assert.ok(Math.abs(end.z) < 1e-6);
});

test("hover cannot tilt the aligned hold or the final approach", () => {
  const pointer = { x: 0.25, y: -0.11 };
  for (const seconds of [0, 0.5, ALIGNMENT_HOLD - 0.001, ROTATION_PERIOD, ROTATION_PERIOD + 0.8]) {
    assert.deepEqual(rotationAt(seconds, pointer), rotationAt(0));
  }
  const end = ROTATION_PERIOD - 0.1;
  assert.deepEqual(rotationAt(end, pointer), rotationAt(end));
  assert.notDeepEqual(rotationAt(8, pointer), rotationAt(8));
});

test("rotation reaches a steady speed immediately after its short ramp", () => {
  const expected = Math.PI * 2 / (ROTATION_PERIOD - ALIGNMENT_HOLD - ROTATION_RAMP);
  for (const time of [ALIGNMENT_HOLD + ROTATION_RAMP, 5, 10, 17]) {
    const speed = (rotationAt(time + 0.01).y - rotationAt(time).y) / 0.01;
    assert.ok(Math.abs(speed - expected) < 1e-10);
  }
  for (const boundary of [ALIGNMENT_HOLD + ROTATION_RAMP, ROTATION_PERIOD - ROTATION_RAMP]) {
    assert.ok(Math.abs(rotationAt(boundary + 0.00001).y - rotationAt(boundary - 0.00001).y) < 0.00001);
  }
});

test("hover blending cannot stall or reverse the automatic turn", () => {
  const speed = Math.PI * 2 / (ROTATION_PERIOD - ALIGNMENT_HOLD - ROTATION_RAMP);
  for (const x of [-HOVER_YAW, HOVER_YAW]) {
    for (let time = ALIGNMENT_HOLD + ROTATION_RAMP; time < ROTATION_PERIOD - ROTATION_RAMP - 0.01; time += 0.01) {
      const velocity = (rotationAt(time + 0.01, { x, y: 0 }).y - rotationAt(time, { x, y: 0 }).y) / 0.01;
      assert.ok(velocity > speed * 0.5);
    }
  }
});

test("fallback stays within a narrow mobile viewport", () => {
  assert.equal(fallback404.split("\n").length, 8);
  assert.ok(fallback404.split("\n").every((line) => line.length <= 42));
});
