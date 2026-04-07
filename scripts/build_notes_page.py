#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import tempfile
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from blog_builder import (
    SITE_FOOTER_OWNER,
    SITE_HEADER_DESCRIPTION,
    SITE_HOME_DESCRIPTION,
    SITE_LANGUAGE,
    SITE_NAME,
    SITE_THEME_COLOR,
    fallback_markdown_to_html,
    find_pandoc,
    join_url,
    parse_date,
    render_sidebar_about_section,
    strip_html,
    write_text,
)

NOTES_MD_DEFAULT = "notes.md"
NOTES_HTML_DEFAULT = "notes.html"
NOTES_JSON_DEFAULT = "notes.json"


def render_markdown_fragment(markdown_text: str, pandoc_path: str | None) -> str:
    if not pandoc_path:
        return fallback_markdown_to_html(markdown_text)

    tmp_md_path: Path | None = None
    tmp_html_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp_md:
            tmp_md_path = Path(tmp_md.name)
            tmp_md.write(markdown_text.encode("utf-8"))

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp_html:
            tmp_html_path = Path(tmp_html.name)

        cmd = [
            pandoc_path,
            "--from",
            "markdown+yaml_metadata_block+tex_math_dollars",
            "--to",
            "html5",
            "--standalone",
            "--output",
            str(tmp_html_path),
            str(tmp_md_path),
        ]
        subprocess.run(cmd, check=True)
        raw_body = tmp_html_path.read_text(encoding="utf-8")
        body_match = re.search(r"(?is)<body[^>]*>(.*?)</body>", raw_body)
        return body_match.group(1) if body_match else raw_body
    except Exception as exc:
        print(f"Pandoc render failed, fallback converter used: {exc}", file=sys.stderr)
        return fallback_markdown_to_html(markdown_text)
    finally:
        if tmp_md_path and tmp_md_path.exists():
            tmp_md_path.unlink()
        if tmp_html_path and tmp_html_path.exists():
            tmp_html_path.unlink()


def _find_level2_headers_outside_fences(text: str) -> list[tuple[int, int, str]]:
    lines = text.split("\n")
    results: list[tuple[int, int, str]] = []
    offset = 0
    in_fence = False
    fence_char = ""

    for line in lines:
      stripped = line.lstrip()
      m_fence = re.match(r"^(```+|~~~+)", stripped)
      if m_fence:
          marker = m_fence.group(1)
          marker_char = marker[0]
          if not in_fence:
              in_fence = True
              fence_char = marker_char
          elif fence_char == marker_char:
              in_fence = False
              fence_char = ""

      if not in_fence:
          m_h2 = re.match(r"^##\s+(.+?)\s*$", line)
          if m_h2:
              start = offset
              end = offset + len(line)
              results.append((start, end, m_h2.group(1).strip()))

      offset += len(line) + 1

    return results


def parse_notes_markdown(raw_text: str, pandoc_path: str | None) -> list[dict[str, Any]]:
    text = raw_text.replace("\r\n", "\n")
    headers = _find_level2_headers_outside_fences(text)
    entries: list[dict[str, Any]] = []

    for idx, header_entry in enumerate(headers):
        _, header_end, header = header_entry
        body_start = header_end + 1
        body_end = headers[idx + 1][0] if idx + 1 < len(headers) else len(text)
        body_md = text[body_start:body_end].strip()
        if not body_md:
            continue

        date_text = ""
        title = ""

        m = re.match(
            r"^(\d{4}[-/.]\d{2}[-/.]\d{2})(?:\s*(?:\||｜|-|—|:|：)\s*(.+))?$",
            header,
        )
        if m:
            date_text = m.group(1)
            title = (m.group(2) or "").strip()
        else:
            m2 = re.match(r"^(\d{4}[-/.]\d{2}[-/.]\d{2})\s+(.+)$", header)
            if m2:
                date_text = m2.group(1)
                title = m2.group(2).strip()
            else:
                date_text = header

        parsed = parse_date(date_text)
        date_norm = parsed.strftime("%Y-%m-%d") if parsed else date_text
        sort_key = parsed or datetime.min

        body_html = render_markdown_fragment(body_md + "\n", pandoc_path)
        summary = strip_html(body_html).strip()
        if len(summary) > 120:
            summary = summary[:120].rstrip() + "..."

        entries.append(
            {
                "date": date_norm,
                "title": title,
                "markdown": body_md,
                "html": body_html,
                "summary": summary,
                "sort_key": sort_key,
            }
        )

    entries.sort(key=lambda x: (x["sort_key"], x["date"], x["title"]), reverse=True)
    for entry in entries:
        entry.pop("sort_key", None)
    return entries


def render_notes_page(site_prefix: str, entries: list[dict[str, Any]]) -> str:
    canonical = join_url(site_prefix, NOTES_HTML_DEFAULT)
    icon = join_url(site_prefix, "assets/img/icon.jpg")
    about_html = render_sidebar_about_section(
        show_about_link=True,
        extra_links=['<a href="/index.html">首页</a>'],
        include_rss=False,
    )

    if entries:
        notes_items = []
        for item in entries:
            title_html = (
                f'<h3 class="note-title">{html.escape(item["title"])}</h3>'
                if item["title"]
                else ""
            )
            notes_items.append(
                "\n".join(
                    [
                        '<article class="note-item">',
                        f'  <time class="note-date" datetime="{html.escape(item["date"])}">{html.escape(item["date"])}</time>',
                        '  <div class="note-body post-content">',
                        f"    {title_html}" if title_html else "",
                        f"    {item['html']}",
                        "  </div>",
                        "</article>",
                    ]
                )
            )
        notes_html = "\n".join(notes_items)
    else:
        notes_html = '<div class="loading">还没有随想，先写下第一条吧。</div>'

    return f"""<!doctype html>
<html lang=\"{SITE_LANGUAGE}\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
    <title>{html.escape(SITE_NAME)} · 每天随想</title>
    <meta name=\"description\" content=\"{html.escape(SITE_HOME_DESCRIPTION)}\" />
    <link rel=\"canonical\" href=\"{html.escape(canonical)}\" />
    <link rel=\"icon\" href=\"/assets/img/icon.jpg\" type=\"image/jpeg\" />
    <link rel=\"apple-touch-icon\" href=\"/assets/img/icon.jpg\" />
    <meta property=\"og:type\" content=\"website\" />
    <meta property=\"og:site_name\" content=\"{SITE_NAME}\" />
    <meta property=\"og:title\" content=\"{html.escape(SITE_NAME)} · 每天随想\" />
    <meta property=\"og:description\" content=\"{html.escape(SITE_HOME_DESCRIPTION)}\" />
    <meta property=\"og:url\" content=\"{html.escape(canonical)}\" />
    <meta property=\"og:image\" content=\"{html.escape(icon)}\" />
    <meta name=\"theme-color\" content=\"{SITE_THEME_COLOR}\" />
    <link rel=\"stylesheet\" href=\"/assets/css/style.css\" />
  </head>
  <body>
    <a class=\"skip-link\" href=\"#notes-list\">跳转到随想时间线</a>
    <div class=\"layout\">
      <aside class=\"sidebar\">
        <header class=\"site-header\">
          <h1 class=\"site-title\">{SITE_NAME}</h1>
          <p class=\"site-desc\">{html.escape(SITE_HEADER_DESCRIPTION)}</p>
        </header>

{about_html}
      </aside>

      <section class=\"content\">
        <aside class=\"directory\" id=\"notes-list\">
          <h2 class=\"dir-title\">每天随想</h2>
          <div class=\"notes-timeline\">
{notes_html}
          </div>
        </aside>
      </section>
    </div>

    <footer class=\"site-footer\">
      <p>© {datetime.now().year} — {html.escape(SITE_FOOTER_OWNER)}</p>
    </footer>
  </body>
</html>
"""


def build_notes_json(entries: list[dict[str, Any]], source_path: Path) -> dict[str, Any]:
    export_entries = [
        {
            "date": item["date"],
            "title": item["title"],
            "summary": item["summary"],
            "contentHtml": item["html"],
        }
        for item in entries
    ]
    return {
        "version": 1,
        "updatedAt": datetime.fromtimestamp(source_path.stat().st_mtime).isoformat(timespec="seconds"),
        "entries": export_entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build notes page and preview data from one markdown file.")
    parser.add_argument("--repo-root", default=".", help="Repository root path.")
    parser.add_argument("--site-url", default="", help="Optional absolute site url for canonical tags.")
    parser.add_argument("--source", default=NOTES_MD_DEFAULT, help="Source markdown file path.")
    parser.add_argument("--output-html", default=NOTES_HTML_DEFAULT, help="Output notes html path.")
    parser.add_argument("--output-json", default=NOTES_JSON_DEFAULT, help="Output notes json path.")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    source_path = repo_root / args.source
    output_html = repo_root / args.output_html
    output_json = repo_root / args.output_json
    site_prefix = args.site_url.strip().rstrip("/")
    pandoc_path = find_pandoc()

    if not source_path.exists():
        raise FileNotFoundError(f"Source markdown not found: {source_path}")

    entries = parse_notes_markdown(source_path.read_text(encoding="utf-8"), pandoc_path)

    html_page = render_notes_page(site_prefix, entries)
    write_text(output_html, html_page)

    notes_json = build_notes_json(entries, source_path)
    output_json.write_text(
        json.dumps(notes_json, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    if pandoc_path:
        print(f"Using pandoc at: {pandoc_path}")
    else:
        print("Pandoc not found; using fallback markdown converter.")
    try:
        out_html_label = str(output_html.relative_to(repo_root))
    except ValueError:
        out_html_label = str(output_html)
    try:
        out_json_label = str(output_json.relative_to(repo_root))
    except ValueError:
        out_json_label = str(output_json)
    print(f"Built {out_html_label} and {out_json_label} with {len(entries)} notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
