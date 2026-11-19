import { MARKER_COLORS } from "./constants.js";

export const fmt = (n) => Number(n).toLocaleString("en-US");

/** A measured amount (minutes, miles) with at most one decimal, e.g. 20.4. */
export const oneDecimal = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 1 });

/** A 0-1 score from the API as a whole number of points out of 100. */
export const points = (score) => Math.round(Number(score) * 100);

export function clampNumber(raw, min, max, fallback, decimals = 0) {
  const n = Number(raw);
  if (raw === null || raw === undefined || raw === "" || !Number.isFinite(n)) return fallback;
  const f = 10 ** decimals;
  return Math.round(Math.min(max, Math.max(min, n)) * f) / f;
}

const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

/** Same ramp as the candidate circle-color expression on the map. */
export function scoreColor(score, theme = "light") {
  const { candidateLow, candidateHigh } = MARKER_COLORS[theme];
  const a = rgb(candidateLow);
  const b = rgb(candidateHigh);
  const t = Math.min(1, Math.max(0, score));
  return "rgb(" + a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(",") + ")";
}

/** Dark text on pale ramp colours, white on strong ones. */
export const scoreTextColor = (score, theme = "light") =>
  score < MARKER_COLORS[theme].darkTextBelow ? "#0f172a" : "#ffffff";
