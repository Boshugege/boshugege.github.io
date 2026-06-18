interface CalendarEvent {
  summary: string;
  location: string;
  start: Date;
  end: Date;
}

function normalizeEvents(payload: unknown): CalendarEvent[] {
  if (!payload || typeof payload !== "object" || !("events" in payload) || !Array.isArray(payload.events)) return [];
  return payload.events.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const event = item as Record<string, unknown>;
    const start = new Date(String(event.start || ""));
    const end = new Date(String(event.end || ""));
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end <= start) return [];
    return [{
      summary: String(event.summary || "").trim(),
      location: String(event.location || "").replace(/\s+/g, " ").trim(),
      start,
      end,
    }];
  }).sort((a, b) => a.start.getTime() - b.start.getTime());
}

function renderEvent(event: CalendarEvent | undefined) {
  const block = document.querySelector<HTMLElement>("[data-next-event]");
  if (!block) return;
  if (!event) {
    block.hidden = true;
    return;
  }

  const day = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const time = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameDay = event.start.toDateString() === event.end.toDateString();
  const timeText = sameDay
    ? `${day.format(event.start)}-${time.format(event.end)}`
    : `${day.format(event.start)}-${day.format(event.end)}`;

  const summary = block.querySelector("[data-event-summary]");
  const eventTime = block.querySelector("[data-event-time]");
  const location = block.querySelector("[data-event-location]");
  if (summary) summary.textContent = event.summary || "忙碌中";
  if (eventTime) eventTime.textContent = timeText;
  if (location) location.textContent = event.location || "（未填写）";
  block.hidden = false;
}

async function initializeNowStatus() {
  const status = document.querySelector<HTMLElement>("[data-now-status]");
  if (!status || status.dataset.loaded === "true") return;
  status.dataset.loaded = "true";
  try {
    const response = await fetch("/now.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`now.json: ${response.status}`);
    const events = normalizeEvents(await response.json());
    const now = new Date();
    const current = events.find((event) => now >= event.start && now < event.end);
    const next = events.find((event) => event.start > now);
    status.textContent = current
      ? current.location ? `${current.summary || "忙碌中"}（${current.location}）` : current.summary || "忙碌中"
      : "空闲";
    renderEvent(next);
  } catch (error) {
    console.warn(error);
    status.textContent = "暂时无法读取状态";
    status.classList.add("is-error");
    renderEvent(undefined);
  }
}

document.addEventListener("astro:page-load", initializeNowStatus);
initializeNowStatus();
