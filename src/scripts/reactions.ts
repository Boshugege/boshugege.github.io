// 观测 buttons (ObserveButton.astro): one reaction per reader per post or
// note. Counts come from one request per page (posts: ?target=, the notes
// page: ?prefix=note: for every note at once). A button stays hidden unless
// that request succeeds. A click fetches a signed challenge, solves the
// proof of work and posts it; the server dedupes by visitor. The browser
// keeps its own reactions in localStorage; what the server reports for the
// visitor's address (possibly someone else on a shared IP) is only shown.
import { solvePow } from "../lib/pow";

const STORAGE_KEY = "pnc-observed";
const REMEMBERED_LIMIT = 500;
const NOTE_PREFIX = "note:";

interface CountsResponse {
  counts?: Record<string, number>;
  observed?: string[];
}

interface ChallengeResponse {
  challenge: string;
  difficulty: number;
}

interface ReactionResponse {
  count: number;
}

function readObserved() {
  try {
    const list: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return new Set(Array.isArray(list) ? list.filter((item): item is string => typeof item === "string") : []);
  } catch {
    return new Set<string>();
  }
}

function remember(target: string) {
  const list = [...readObserved()].filter((item) => item !== target);
  list.push(target);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(-REMEMBERED_LIMIT)));
  } catch {
    // Private mode or full storage: the server still remembers.
  }
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, credentials: "same-origin" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body?.error === "string" ? body.error : `HTTP ${response.status}`);
  return body as T;
}

function render(button: HTMLButtonElement, count: number, observed: boolean) {
  button.hidden = false;
  button.dataset.observeValue = String(count);
  button.setAttribute("aria-pressed", String(observed));
  // One reaction per reader: an observed button cannot be pressed again.
  if (observed) button.setAttribute("aria-disabled", "true");
  else button.removeAttribute("aria-disabled");
  button.classList.toggle("is-observed", observed);
  const label = button.querySelector<HTMLElement>("[data-observe-label]");
  const counter = button.querySelector<HTMLElement>("[data-observe-count]");
  if (label) label.textContent = observed ? "已观测" : "观测";
  if (counter) counter.textContent = count.toLocaleString("zh-CN");
  button.title = observed ? `你已观测过（共 ${count} 次观测）` : `观测一下（已有 ${count} 次观测）`;
}

async function hydrate() {
  const buttons = [...document.querySelectorAll<HTMLButtonElement>("[data-observe]:not([data-observe-bound])")];
  if (!buttons.length) return;
  for (const button of buttons) button.dataset.observeBound = "";
  const apiBase = buttons[0].dataset.apiBase || "/api";
  const targets = buttons.map((button) => button.dataset.observeTarget || "");
  const noteTargets = targets.some((target) => target.startsWith(NOTE_PREFIX));
  const queries = [
    ...(noteTargets ? [`prefix=${encodeURIComponent(NOTE_PREFIX)}`] : []),
    ...new Set(targets.filter((target) => target && !target.startsWith(NOTE_PREFIX)).map((target) => `target=${encodeURIComponent(target)}`)),
  ];
  const results = await Promise.allSettled(queries.map((query) => fetchJson<CountsResponse>(`${apiBase}/reactions?${query}`)));
  const counts = new Map<string, number>();
  const observed = readObserved();
  let answered = false;
  const coveredTargets = new Set<string>();
  results.forEach((result, index) => {
    if (result.status !== "fulfilled") return;
    answered = true;
    const query = queries[index];
    const answer = result.value.counts || {};
    // A prefix answer lists only notes with reactions (the rest are 0). A
    // single-target answer always includes its target unless the server
    // cannot vouch for it (e.g. it has not seen the post yet): stay hidden.
    if (query.startsWith("prefix=")) targets.filter((target) => target.startsWith(NOTE_PREFIX)).forEach((target) => coveredTargets.add(target));
    else {
      const target = decodeURIComponent(query.slice("target=".length));
      if (Object.hasOwn(answer, target)) coveredTargets.add(target);
    }
    for (const [target, count] of Object.entries(answer)) counts.set(target, Number(count) || 0);
    for (const target of result.value.observed || []) observed.add(target);
  });
  if (!answered) return;
  buttons.forEach((button, index) => {
    const target = targets[index];
    if (coveredTargets.has(target)) render(button, counts.get(target) || 0, observed.has(target));
  });
}

function flash(button: HTMLButtonElement, message: string) {
  const label = button.querySelector<HTMLElement>("[data-observe-label]");
  if (!label) return;
  label.textContent = message;
  window.setTimeout(() => {
    if (!button.isConnected || button.dataset.busy === "1") return;
    render(button, Number(button.dataset.observeValue) || 0, button.getAttribute("aria-pressed") === "true");
  }, 2400);
}

async function observe(button: HTMLButtonElement) {
  const target = button.dataset.observeTarget;
  const apiBase = button.dataset.apiBase || "/api";
  if (!target || button.dataset.busy === "1" || button.getAttribute("aria-pressed") === "true") return;
  button.dataset.busy = "1";
  button.setAttribute("aria-busy", "true");
  const label = button.querySelector<HTMLElement>("[data-observe-label]");
  if (label) label.textContent = "观测中…";
  try {
    const challenge = await fetchJson<ChallengeResponse>(`${apiBase}/reactions/challenge?target=${encodeURIComponent(target)}`);
    const nonce = await solvePow(challenge.challenge, challenge.difficulty);
    const result = await fetchJson<ReactionResponse>(`${apiBase}/reactions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ target, challenge: challenge.challenge, nonce }),
    });
    remember(target);
    delete button.dataset.busy;
    render(button, Number(result.count) || 0, true);
    button.classList.remove("just-observed");
    void button.offsetWidth;
    button.classList.add("just-observed");
  } catch {
    delete button.dataset.busy;
    render(button, Number(button.dataset.observeValue) || 0, false);
    flash(button, "稍后再试");
  } finally {
    button.removeAttribute("aria-busy");
  }
}

// One listener for every page (it survives Astro page swaps).
document.addEventListener("click", (event) => {
  const button = (event.target as Element | null)?.closest<HTMLButtonElement>("[data-observe]");
  if (button) void observe(button);
});
document.addEventListener("astro:page-load", () => void hydrate());
void hydrate();
