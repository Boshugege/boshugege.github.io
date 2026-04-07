#!/usr/bin/env python3
"""Fetch iCloud published calendar and generate deterministic now.json."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CALENDAR_URL = (
    "webcal://p227-caldav.icloud.com.cn/published/2/"
    "MTgzMjM1MzA0MDIxODMyM_UREbj5PQcyA96FL7MJBF5qiBpYgUNwqLGCZ9JVR1uK6ofFHRNgbwLVIPJ0VAXBYv5xfH8sji-2YGKEPySJdEM"
)


@dataclass
class Event:
    summary: str = ""
    location: str = ""
    dtstart: datetime | None = None
    dtend: datetime | None = None
    rrule: Dict[str, str] = field(default_factory=dict)
    exdates: List[datetime] = field(default_factory=list)


def normalize_calendar_url(url: str) -> str:
    value = (url or "").strip()
    if value.startswith("webcal://"):
        return "https://" + value[len("webcal://") :]
    return value


def fetch_calendar_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "now-status-bot/1.0 (+https://github.com/Boshugege/boshugege.github.io)"
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in body:
        raise RuntimeError("Calendar response is not a valid VCALENDAR")
    return body


def unfold_ical_lines(raw: str) -> List[str]:
    lines = raw.replace("\r\n", "\n").split("\n")
    out: List[str] = []
    for line in lines:
        if not out:
            out.append(line)
            continue
        if line.startswith(" ") or line.startswith("\t"):
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def split_ical_line(line: str):
    idx = line.find(":")
    if idx <= 0:
        return None
    left = line[:idx]
    value = line[idx + 1 :]
    parts = left.split(";")
    name = parts[0].upper()
    params: Dict[str, str] = {}
    for part in parts[1:]:
        eq = part.find("=")
        if eq <= 0:
            continue
        params[part[:eq].upper()] = part[eq + 1 :]
    return name, params, value


def unescape_ical_text(value: str) -> str:
    return (
        (value or "")
        .replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_ical_dt(raw_value: str, params: Dict[str, str]) -> datetime | None:
    value = (raw_value or "").strip()
    if not value:
        return None

    value_type = params.get("VALUE", "").upper()
    tzid = params.get("TZID", "")

    if value_type == "DATE":
        m = re.match(r"^(\d{4})(\d{2})(\d{2})$", value)
        if not m:
            return None
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(y, mo, d, 0, 0, 0, tzinfo=SH_TZ)

    m_utc = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?Z$", value)
    if m_utc:
        y, mo, d = int(m_utc.group(1)), int(m_utc.group(2)), int(m_utc.group(3))
        hh, mm, ss = int(m_utc.group(4)), int(m_utc.group(5)), int(m_utc.group(6) or 0)
        return datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc).astimezone(SH_TZ)

    m_local = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?$", value)
    if not m_local:
        return None

    y, mo, d = int(m_local.group(1)), int(m_local.group(2)), int(m_local.group(3))
    hh, mm, ss = int(m_local.group(4)), int(m_local.group(5)), int(m_local.group(6) or 0)

    if tzid and tzid != "Asia/Shanghai":
        return datetime(y, mo, d, hh, mm, ss, tzinfo=SH_TZ)
    return datetime(y, mo, d, hh, mm, ss, tzinfo=SH_TZ)


def parse_rrule(value: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (value or "").split(";"):
        idx = part.find("=")
        if idx <= 0:
            continue
        out[part[:idx].upper()] = part[idx + 1 :]
    return out


def parse_exdates(value: str, params: Dict[str, str]) -> List[datetime]:
    out = []
    for item in (value or "").split(","):
        dt = parse_ical_dt(item, params)
        if dt is not None:
            out.append(dt)
    return out


def parse_events(raw_text: str) -> List[Event]:
    events: List[Event] = []
    current: Event | None = None

    for line in unfold_ical_lines(raw_text):
        if line == "BEGIN:VEVENT":
            current = Event()
            continue
        if line == "END:VEVENT":
            if current and current.dtstart:
                if not current.dtend:
                    current.dtend = current.dtstart + timedelta(hours=1)
                if current.dtend > current.dtstart:
                    events.append(current)
            current = None
            continue

        if current is None:
            continue

        parsed = split_ical_line(line)
        if not parsed:
            continue

        name, params, value = parsed
        if name == "SUMMARY":
            current.summary = unescape_ical_text(value)
        elif name == "LOCATION":
            current.location = unescape_ical_text(value)
        elif name == "DTSTART":
            current.dtstart = parse_ical_dt(value, params)
        elif name == "DTEND":
            current.dtend = parse_ical_dt(value, params)
        elif name == "RRULE":
            current.rrule = parse_rrule(value)
        elif name == "EXDATE":
            current.exdates.extend(parse_exdates(value, params))

    return events


def parse_until(rrule: Dict[str, str]) -> datetime | None:
    value = rrule.get("UNTIL", "")
    if not value:
        return None
    return parse_ical_dt(value, {})


def is_excluded(exdates: List[datetime], start: datetime) -> bool:
    target = start.timestamp()
    return any(abs(d.timestamp() - target) < 1 for d in exdates)


def overlaps(start: datetime, end: datetime, window_start: datetime, window_end: datetime) -> bool:
    return end > window_start and start < window_end


def expand_event_occurrences(event: Event, window_start: datetime, window_end: datetime) -> List[tuple[datetime, datetime]]:
    if not event.dtstart or not event.dtend:
        return []

    if event.dtend <= event.dtstart:
        return []

    rrule = event.rrule
    freq = rrule.get("FREQ", "").upper()

    if freq != "WEEKLY":
        if is_excluded(event.exdates, event.dtstart):
            return []
        if overlaps(event.dtstart, event.dtend, window_start, window_end):
            return [(event.dtstart, event.dtend)]
        return []

    until = parse_until(rrule)
    interval = max(1, int(rrule.get("INTERVAL", "1") or "1"))
    duration = event.dtend - event.dtstart
    if duration.total_seconds() <= 0:
        return []

    week_seconds = 7 * 24 * 3600
    diff_seconds = (window_start - event.dtstart).total_seconds()
    base_weeks = int(diff_seconds // week_seconds)
    if base_weeks < 0:
        base_weeks = 0

    step = interval
    if base_weeks % step != 0:
        base_weeks += step - (base_weeks % step)

    results: List[tuple[datetime, datetime]] = []
    weeks = base_weeks
    max_loops = 4096

    for _ in range(max_loops):
        candidate_start = event.dtstart + timedelta(days=7 * weeks)
        candidate_end = candidate_start + duration

        if candidate_start >= window_end:
            break

        if until and candidate_start > until:
            break

        if not is_excluded(event.exdates, candidate_start) and overlaps(
            candidate_start, candidate_end, window_start, window_end
        ):
            results.append((candidate_start, candidate_end))

        weeks += step

    return results


def iso_local(dt: datetime) -> str:
    return dt.astimezone(SH_TZ).isoformat(timespec="seconds")


def build_payload(events: List[Event], source_url: str) -> Dict[str, object]:
    now = datetime.now(SH_TZ)
    window_start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=SH_TZ)
    window_end = window_start + timedelta(days=7)

    out_events = []
    seen = set()

    for event in events:
        summary = (event.summary or "忙碌中").strip()
        location = re.sub(r"\s+", " ", (event.location or "")).strip()
        for start, end in expand_event_occurrences(event, window_start, window_end):
            key = (summary, location, start.timestamp(), end.timestamp())
            if key in seen:
                continue
            seen.add(key)
            out_events.append(
                {
                    "summary": summary,
                    "location": location,
                    "start": iso_local(start),
                    "end": iso_local(end),
                }
            )

    out_events.sort(key=lambda x: (x["start"], x["end"], x["summary"], x["location"]))

    return {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "windowStart": iso_local(window_start),
        "windowEnd": iso_local(window_end),
        "source": source_url,
        "events": out_events,
    }


def main() -> int:
    calendar_url = normalize_calendar_url(os.getenv("CALENDAR_URL") or DEFAULT_CALENDAR_URL)
    output_path = Path(os.getenv("NOW_JSON_PATH", "now.json"))

    raw = fetch_calendar_text(calendar_url)
    events = parse_events(raw)
    payload = build_payload(events, calendar_url)

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output_path} with {len(payload['events'])} events")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
