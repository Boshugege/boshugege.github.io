export type Point = readonly [number, number];

export interface Fragment {
  outline: readonly Point[];
  x: number;
  z: number;
  thickness: number;
}

const four: readonly (readonly Point[])[] = [
  [[-0.63, 0.935], [-0.1, 1.95], [0.59, 1.95], [0.06, 0.935]],
  [[-1.16, -0.08], [-0.63, 0.935], [0.06, 0.935], [-0.47, -0.08]],
  [[-1.16, -0.69], [1.24, -0.69], [1.24, -0.08], [-1.16, -0.08]],
  [[0.48, -0.08], [1.09, -0.08], [1.09, 1.95], [0.48, 1.95]],
  [[0.48, -1.32], [1.09, -1.32], [1.09, -0.69], [0.48, -0.69]],
  [[0.48, -1.95], [1.09, -1.95], [1.09, -1.32], [0.48, -1.32]],
];

const outer: readonly Point[] = [
  [-0.65, 1.95], [0.65, 1.95], [1.23, 1.37], [1.23, -1.37],
  [0.65, -1.95], [-0.65, -1.95], [-1.23, -1.37], [-1.23, 1.37],
];
const inner: readonly Point[] = [
  [-0.36, 1.36], [0.36, 1.36], [0.62, 1.1], [0.62, -1.1],
  [0.36, -1.36], [-0.36, -1.36], [-0.62, -1.1], [-0.62, 1.1],
];

const depths = [-1.9, 1.25, -0.55, 2.15, -1.25, 0.6, -2.25, 1.6];

export function createFragments(): Fragment[] {
  const zero = outer.map((point, i) => {
    const next = (i + 1) % outer.length;
    return [point, outer[next], inner[next], inner[i]];
  });

  return [four, zero, four].flatMap((outlines, digit) =>
    outlines.map((outline, i) => ({
      outline,
      x: (digit - 1) * 3.35,
      z: depths[(i + digit * 3) % depths.length],
      thickness: 0.55 + (i % 3) * 0.18,
    })),
  );
}

export const ASCII_CHARACTERS = " .`'^\",:;Il!i~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$";
export const asciiCellSize = (width: number) => width < 600 ? 4.5 : 6;

export const ROTATION_PERIOD = 20;
export const ALIGNMENT_HOLD = 1.5;
export const ROTATION_RAMP = 0.55;
export const HOVER_YAW = 0.14;
export const HOVER_TILT = 0.08;

export function rotationAt(seconds: number, offset = { x: 0, y: 0 }) {
  const phase = ((seconds % ROTATION_PERIOD) + ROTATION_PERIOD) % ROTATION_PERIOD;
  const elapsed = Math.max(0, phase - ALIGNMENT_HOLD);
  const duration = ROTATION_PERIOD - ALIGNMENT_HOLD;
  const speed = Math.PI * 2 / (duration - ROTATION_RAMP);
  let y: number;
  if (elapsed < ROTATION_RAMP) {
    y = speed * elapsed * elapsed / (2 * ROTATION_RAMP);
  } else if (elapsed > duration - ROTATION_RAMP) {
    y = Math.PI * 2 - speed * (duration - elapsed) ** 2 / (2 * ROTATION_RAMP);
  } else {
    y = speed * (elapsed - ROTATION_RAMP / 2);
  }

  // Lock the actual projection, including hover, throughout the front-facing hold.
  const distance = Math.min(y, Math.PI * 2 - y);
  const weight = Math.min(1, Math.max(0, (distance - 0.12) / 0.43));
  const hover = weight * weight * (3 - 2 * weight);
  return {
    x: Math.sin(y) * 0.28 + offset.y * hover,
    y: y + offset.x * hover,
    z: Math.sin(y) * 0.09,
    aligned: elapsed === 0,
  };
}

export const fallback404 = [
  "    ##    ##      ######         ##    ##",
  "   ##     ##    ##      ##      ##     ##",
  "  ##      ##   ##        ##    ##      ##",
  " ##       ##   ##        ##   ##       ##",
  "#############  ##        ##  #############",
  "          ##   ##        ##            ##",
  "          ##    ##      ##             ##",
  "          ##      ######               ##",
].join("\n");
