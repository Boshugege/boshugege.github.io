#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


# ========== Editable Configuration ==========
SITE_NAME = "PNC's Blog"
SITE_HOME_DESCRIPTION = "一个随意写写的简单博客"
SITE_HEADER_DESCRIPTION = "ParityNonconservation's personal blog, a simple static site."
SITE_THEME_COLOR = "#f2efe7"
SITE_LANGUAGE = "zh-CN"
SITE_FOOTER_OWNER = "ParityNonconservation"
SITE_RSS_DESCRIPTION = "静态 HTML 技术博客 RSS"

ABOUT_TITLE = "About Me"
ABOUT_LINES = [
    "一个正在学习的小朋友。",
    "Tsinghua University",
    "Beijing, China",
    "Tech and something else",
]
CONTACT_EMAIL = "admin@parityncsvt.top"
CONTACT_GITHUB = "https://github.com/Boshugege"

DEFAULT_COVER_RELATIVE = "assets/img/icon.jpg"
MANIFEST_RELATIVE = "manifest.webmanifest"

# Tag pages can still be generated, but are excluded from sitemap by default.
GENERATE_TAG_PAGES = False
INCLUDE_TAG_PAGES_IN_SITEMAP = False
SITEMAP_ROOT_PAGES = ("index.html", "about.html", "rss.xml")

POSTS_DIRNAME = "posts"
DRAFTS_DIRNAME = "drafts"
TAGS_DIRNAME = "tags"
INDEX_JSON_NAME = "index.json"
SEARCH_JSON_NAME = "search.json"
INDEX_HTML_NAME = "index.html"
RSS_XML_NAME = "rss.xml"
SITEMAP_XML_NAME = "sitemap.xml"
ABOUT_HTML_NAME = "about.html"

DEFAULT_DRAFT_GLOB = "*.md"
PANDOC_HIGHLIGHT_STYLE = "tango"


POST_HEAD_EXTRAS = """
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/styles/default.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/highlight.min.js" defer></script>
  <script>
    document.addEventListener('DOMContentLoaded',()=>{ document.querySelectorAll('pre code').forEach((el)=> { if(window.hljs) hljs.highlightElement(el); }); });
  </script>

  <script>
    window.MathJax = { tex: { inlineMath: [['$','$'], ['\\\\(','\\\\)']] } };
  </script>
  <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" defer></script>

  <script>
    // Enhance code blocks: detect language from classes and set data-lang on <pre>
    // Only set data-lang when language is explicitly specified (no icon)
    document.addEventListener('DOMContentLoaded', ()=>{
      document.querySelectorAll('pre').forEach(pre => {
        if (pre.hasAttribute('data-lang')) return; // only set when not already set
        const code = pre.querySelector('code');
        if (!code) return;
        let lang = '';
        // class patterns: language-xxx, sourceCode xxx, or plain xxx
        code.classList.forEach(c => {
          if (c.startsWith('language-')) lang = c.replace('language-','');
          else if (!lang && /^[a-z]+$/i.test(c) && c.length < 20) lang = c;
        });
        // handle pandoc's div.sourceCode parent classes
        const parentDiv = pre.closest('div.sourceCode');
        if (!lang && parentDiv) {
          parentDiv.classList.forEach(c => { if (c !== 'sourceCode' && /^[a-z]+$/i.test(c)) lang = c });
        }
        // fallback: look for first language-... in entire pre
        if (!lang){ const m = pre.innerHTML.match(/class=\\"[^\\"]*language-([a-zA-Z0-9]+)[^\\"]*\\"/); if(m) lang = m[1]; }

        if (lang) {
          const normalized = lang.toLowerCase();
          // only show if language was actually specified (we detect common patterns above)
          const display = normalized.replace(/^[a-z]/, (s) => s.toUpperCase());
          pre.setAttribute('data-lang', display);
        }
      });
    });
  </script>
""".strip("\n")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def normalize_url_path(value: str) -> str:
    return value.replace("\\", "/")


def join_url(prefix: str, relative_path: str) -> str:
    normalized = relative_path.lstrip("/")
    if not prefix:
        return f"/{normalized}" if normalized else "/"
    prefix = prefix.rstrip("/")
    return f"{prefix}/{normalized}" if normalized else prefix


def resolve_site_prefix(repo_root: Path, cli_site_url: str) -> str:
    if cli_site_url.strip():
        return cli_site_url.strip().rstrip("/")

    env_site_url = os.getenv("SITE_URL", "").strip()
    if env_site_url:
        return env_site_url.rstrip("/")

    cname_path = repo_root / "CNAME"
    if cname_path.exists():
        domain = read_text(cname_path).strip()
        if domain:
            if re.match(r"^https?://", domain, flags=re.IGNORECASE):
                return domain.rstrip("/")
            return f"https://{domain.rstrip('/')}"
    return ""


def resolve_cover_path(cover_value: str, default_relative: str) -> str:
    value = (cover_value or "").strip()
    if not value:
        return default_relative
    if re.match(r"^(https?:)?//", value, flags=re.IGNORECASE):
        return value
    return value.lstrip("/")


def resolve_cover_url(cover_path: str, site_prefix: str) -> str:
    if not cover_path:
        return ""
    if re.match(r"^https?://", cover_path, flags=re.IGNORECASE):
        return cover_path
    if cover_path.startswith("//"):
        return f"https:{cover_path}"
    return join_url(site_prefix, cover_path)


def parse_date(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def stable_id(value: str) -> str:
    digest = hashlib.sha256(value.lower().encode("utf-8")).hexdigest()
    return digest[:32]


def strip_html(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    text = re.sub(r"\s+", " ", text)
    return html.unescape(text).strip()


def extract_search_content(html_text: str) -> str:
    article_html = html_text
    article_match = re.search(
        r'<article\s+class="post-content"[^>]*>([\s\S]*?)</article>',
        html_text,
        flags=re.IGNORECASE,
    )
    if article_match:
        article_html = article_match.group(1)

    article_html = re.sub(
        r"<script[^>]*>[\s\S]*?</script>", " ", article_html, flags=re.IGNORECASE
    )
    article_html = re.sub(
        r"<style[^>]*>[\s\S]*?</style>", " ", article_html, flags=re.IGNORECASE
    )
    return strip_html(article_html)


def get_meta_value(html_text: str, name: str) -> str:
    pattern = re.compile(
        r'<meta\b[^>]*\bname=["\']'
        + re.escape(name)
        + r'["\'][^>]*\bcontent=["\']([^"\']*)["\']',
        flags=re.IGNORECASE,
    )
    match = pattern.search(html_text)
    if not match:
        return ""
    return html.unescape(match.group(1).strip())


def slugify(value: str) -> str:
    out = (value or "").strip().lower()
    out = re.sub(r"[^\w\s-]", "", out, flags=re.UNICODE)
    out = re.sub(r"[\s_]+", "-", out, flags=re.UNICODE)
    out = out.strip("-")
    return out


def slugify_tag(tag: str) -> str:
    out = tag.strip().lower()
    out = re.sub(r"\s+", "-", out)
    out = re.sub(r'[\\/:*?"<>|#%&{}$!@+`=]', "", out)
    out = out.strip("-")
    if not out:
        return f"tag-{stable_id(tag)[:10]}"
    return out


def normalize_tags(meta_value: Any) -> list[str]:
    if meta_value is None:
        return []
    if isinstance(meta_value, list):
        values = [str(x).strip() for x in meta_value]
    else:
        values = [s.strip() for s in str(meta_value).split(",")]
    return [v for v in values if v]


def parse_front_matter(source_text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^\s*---\s*\n([\s\S]*?)\n---\s*\n?", source_text)
    if not match:
        return {}, source_text
    yaml_text = match.group(1)
    rest = source_text[match.end() :]

    # Try PyYAML first (optional), then fallback to a simple parser.
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(yaml_text) or {}
        if isinstance(parsed, dict):
            return parsed, rest
    except Exception:
        pass

    parsed: dict[str, Any] = {}
    lines = yaml_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        key_match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not key_match:
            i += 1
            continue
        key = key_match.group(1)
        value = key_match.group(2).strip()
        if value:
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            parsed[key] = value
            i += 1
            continue

        i += 1
        arr: list[str] = []
        while i < len(lines):
            arr_match = re.match(r"^\s*-\s*(.+)$", lines[i].rstrip())
            if not arr_match:
                break
            arr.append(arr_match.group(1).strip().strip('"').strip("'"))
            i += 1
        if arr:
            parsed[key] = arr
        else:
            parsed[key] = ""
    return parsed, rest


def find_pandoc() -> str | None:
    env_path = os.getenv("PANDOC_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    which_path = shutil.which("pandoc")
    if which_path:
        return which_path

    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidate = Path(local_app_data) / "Pandoc" / "pandoc.exe"
        if candidate.exists():
            return str(candidate)
    return None


def run_pandoc(source_file: Path, pandoc_path: str) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp:
        tmp_path = Path(tmp.name)
    try:
        cmd = [
            pandoc_path,
            "--from",
            "markdown+yaml_metadata_block+tex_math_dollars",
            "--to",
            "html",
            "--mathjax",
            "--syntax-highlighting",
            PANDOC_HIGHLIGHT_STYLE,
            "-o",
            str(tmp_path),
            str(source_file),
        ]
        subprocess.run(cmd, check=True)
        raw_body = read_text(tmp_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    body_match = re.search(r"(?is)<body[^>]*>(.*?)</body>", raw_body)
    if body_match:
        return body_match.group(1)
    return raw_body


def fallback_markdown_to_html(markdown_text: str) -> str:
    text = markdown_text.replace("\r\n", "\n")

    def code_block_repl(match: re.Match[str]) -> str:
        lang = match.group(1).strip()
        code = html.escape(match.group(2))
        if lang:
            return f'<pre><code class="language-{lang}">{code}</code></pre>'
        return f"<pre><code>{code}</code></pre>"

    text = re.sub(r"```([a-zA-Z0-9_-]*)\n([\s\S]*?)\n```", code_block_repl, text)
    text = re.sub(
        r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", text
    )
    text = re.sub(r"^###\s*(.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^##\s*(.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s*(.+)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    parts = re.split(r"\n{2,}", text)
    out: list[str] = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if re.match(r"^\$\$[\s\S]*\$\$$", p):
            out.append(p)
        elif p.startswith("<"):
            out.append(p)
        else:
            out.append(f"<p>{p.replace(chr(10), '<br/>')}</p>")
    return "\n".join(out)


def build_tag_anchors(tags: list[str]) -> str:
    if not tags:
        return ""
    anchors = [
        f'<a href="/index.html?tag={quote(tag)}">{html.escape(tag)}</a>' for tag in tags
    ]
    return ", ".join(anchors)


def to_rfc2822_date(value: str, fallback_utc: datetime) -> str:
    dt = parse_date(value)
    if not dt:
        return format_datetime(fallback_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc))


def file_lastmod(path: Path) -> str:
    if not path.exists():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def post_lastmod(post_date: str, fallback_path: Path) -> str:
    dt = parse_date(post_date)
    if dt:
        return dt.strftime("%Y-%m-%d")
    return file_lastmod(fallback_path)


def convert_draft_to_post(
    source_file: Path, repo_root: Path, site_prefix: str, pandoc_path: str | None
) -> Path:
    raw = read_text(source_file)
    meta, markdown_body = parse_front_matter(raw)

    title = str(meta.get("title") or source_file.stem)
    input_date = str(meta.get("date") or datetime.now().strftime("%Y-%m-%d"))
    parsed_date = parse_date(input_date)
    date_str = (
        parsed_date.strftime("%Y-%m-%d")
        if parsed_date
        else datetime.now().strftime("%Y-%m-%d")
    )
    tags = normalize_tags(meta.get("tags", ""))
    tags_str = ",".join(tags)
    excerpt = str(meta.get("excerpt") or "").strip()
    cover_raw = str(meta.get("cover") or "").strip()
    slug_source = str(meta.get("slug") or source_file.stem).strip()
    slug = slugify(slug_source) or source_file.stem.lower()

    out_filename = f"{date_str}-{slug}.html"
    out_path = repo_root / POSTS_DIRNAME / out_filename
    ensure_dir(out_path.parent)

    relative_post_url = f"{POSTS_DIRNAME}/{out_filename}"
    canonical_url = join_url(site_prefix, relative_post_url)
    cover_path = resolve_cover_path(cover_raw, DEFAULT_COVER_RELATIVE)
    og_image = resolve_cover_url(cover_path, site_prefix)

    if pandoc_path:
        body_html = run_pandoc(source_file, pandoc_path)
    else:
        body_html = fallback_markdown_to_html(markdown_body)

    if not excerpt:
        excerpt = strip_html(body_html)[:180]

    tag_anchors = build_tag_anchors(tags)
    meta_line = (
        f'    <p class="meta">{html.escape(date_str)} · 标签：{tag_anchors}</p>\n'
    )

    headline_json = json.dumps(title, ensure_ascii=False)

    doc = f"""<!doctype html>
<html lang="{SITE_LANGUAGE}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(excerpt)}">
  <link rel="canonical" href="{html.escape(canonical_url)}">
  <link rel="manifest" href="/{MANIFEST_RELATIVE}">
  <link rel="icon" href="/assets/img/icon.jpg" type="image/jpeg">
  <link rel="apple-touch-icon" href="/assets/img/icon.jpg">
  <meta property="og:type" content="article">
  <meta property="og:title" content="{html.escape(title)}">
  <meta property="og:description" content="{html.escape(excerpt)}">
  <meta property="og:url" content="{html.escape(canonical_url)}">
  <meta property="og:image" content="{html.escape(og_image)}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{html.escape(title)}">
  <meta name="twitter:description" content="{html.escape(excerpt)}">
  <meta name="twitter:image" content="{html.escape(og_image)}">
  <meta name="post-title" content="{html.escape(title)}">
  <meta name="post-date" content="{html.escape(date_str)}">
  <meta name="post-tags" content="{html.escape(tags_str)}">
  <meta name="post-excerpt" content="{html.escape(excerpt)}">
  <meta name="post-cover" content="{html.escape(cover_path)}">
  <meta name="post-url" content="{html.escape(relative_post_url)}">
  <link rel="stylesheet" href="/assets/css/style.css">
{POST_HEAD_EXTRAS}
  <script type="application/ld+json">
    {{
      "@context": "https://schema.org",
      "@type": "BlogPosting",
      "headline": {headline_json},
      "datePublished": "{html.escape(date_str)}",
      "url": "{html.escape(canonical_url)}",
      "image": "{html.escape(og_image)}"
    }}
  </script>
</head>
<body>
  <article class="post-content">
    <h1>{html.escape(title)}</h1>
{meta_line}{body_html}
  </article>
</body>
</html>
"""
    write_text(out_path, doc)
    return out_path


def render_tag_nav(posts: list[dict[str, Any]], active_tag: str) -> str:
    tag_map: dict[str, int] = {}
    for post in posts:
        for tag in post["tags"]:
            tag_map[tag] = tag_map.get(tag, 0) + 1

    sorted_tags = sorted(tag_map.items(), key=lambda kv: (-kv[1], kv[0]))
    all_class = ' class="tag top active"' if not active_tag else ' class="tag top"'
    nav = [f'<a href="/index.html"{all_class}>全部</a>']
    for tag, count in sorted_tags:
        active = ' class="active"' if tag == active_tag else ""
        nav.append(
            f'<a href="/index.html?tag={quote(tag)}"{active}>{html.escape(tag)} ({count})</a>'
        )
    return "".join(nav)


def render_post_list(posts: list[dict[str, Any]]) -> str:
    if not posts:
        return '<div class="dir-list loading">没有找到相关文章。</div>'

    parts = ['<div class="dir-list">']
    for post in posts:
        parts.append("<div class=\"dir-item\">")
        parts.append(
            f'  <a href="/{html.escape(post["url"])}">{html.escape(post["title"])}</a>'
        )
        parts.append(f'  <span class="meta">{html.escape(post["date"])}</span>')
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts)


def render_sidebar_about_section(
    *,
    show_about_link: bool,
    extra_links: list[str] | None = None,
    include_rss: bool,
) -> str:
    about_intro = ABOUT_LINES[0] if len(ABOUT_LINES) > 0 else ""
    profile_1 = ABOUT_LINES[1] if len(ABOUT_LINES) > 1 else ""
    profile_2 = ABOUT_LINES[2] if len(ABOUT_LINES) > 2 else ""
    profile_3 = ABOUT_LINES[3] if len(ABOUT_LINES) > 3 else ""
    extra = extra_links or []
    contact_links = [*extra]
    contact_links.append(f'<a href="mailto:{html.escape(CONTACT_EMAIL)}">Email</a>')
    if include_rss:
        contact_links.append(f'<a href="/{RSS_XML_NAME}">RSS</a>')
    contact_links.append(f'<a href="{html.escape(CONTACT_GITHUB)}">GitHub</a>')
    more_link = (
        f' <a class="about-more-link" href="/{ABOUT_HTML_NAME}">更多</a>'
        if show_about_link
        else ""
    )

    return f"""        <section class="about">
          <h2><a class="plain-link" href="/{ABOUT_HTML_NAME}">{html.escape(ABOUT_TITLE)}</a></h2>
          <p>{html.escape(about_intro)}{more_link}</p>
          <p class="profile-info">{html.escape(profile_1)}</p>
          <p class="profile-info">{html.escape(profile_2)}</p>
          <p class="profile-info">{html.escape(profile_3)}</p>
          <p class="profile-info now-status">
            我正在：<span id="now-status-text">读取中...</span>
          </p>
          <div id="next-event" class="profile-info next-event" hidden>
            <p class="next-event-title">下一个日程：<span id="next-event-summary"></span></p>
            <p class="next-event-time">时间：<span id="next-event-time"></span></p>
            <p class="next-event-location">地点：<span id="next-event-location"></span></p>
          </div>
          <p class="contact">
            {" · ".join(contact_links)}
          </p>
        </section>"""


def render_shell(
    site_prefix: str,
    page_title: str,
    description: str,
    canonical_path: str,
    tag_nav_html: str,
    main_html: str,
    extra_head: str,
    json_ld: str,
    include_client_script: bool,
    forced_tag: str,
) -> str:
    canonical_url = join_url(site_prefix, canonical_path)
    og_image_url = resolve_cover_url(DEFAULT_COVER_RELATIVE, site_prefix)
    rss_url = join_url(site_prefix, RSS_XML_NAME)
    manifest_url = join_url(site_prefix, MANIFEST_RELATIVE)
    script_line = '    <script src="/assets/js/site.js" defer></script>' if include_client_script else ""
    about_html = render_sidebar_about_section(
        show_about_link=True,
        extra_links=[],
        include_rss=True,
    )

    return f"""<!doctype html>
<html lang="{SITE_LANGUAGE}">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{html.escape(page_title)}</title>
    <meta name="description" content="{html.escape(description)}" />
    <link rel="canonical" href="{html.escape(canonical_url)}" />
    <link rel="alternate" type="application/rss+xml" title="{SITE_NAME} RSS" href="{html.escape(rss_url)}" />
    <link rel="manifest" href="{html.escape(manifest_url)}" />
    <link rel="icon" href="/assets/img/icon.jpg" type="image/jpeg" />
    <link rel="apple-touch-icon" href="/assets/img/icon.jpg" />
    <meta property="og:type" content="website" />
    <meta property="og:site_name" content="{SITE_NAME}" />
    <meta property="og:title" content="{html.escape(page_title)}" />
    <meta property="og:description" content="{html.escape(description)}" />
    <meta property="og:url" content="{html.escape(canonical_url)}" />
    <meta property="og:image" content="{html.escape(og_image_url)}" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="{html.escape(page_title)}" />
    <meta name="twitter:description" content="{html.escape(description)}" />
    <meta name="twitter:image" content="{html.escape(og_image_url)}" />
    <meta name="theme-color" content="{SITE_THEME_COLOR}" />
    <link rel="stylesheet" href="/assets/css/style.css" />
{extra_head}
  </head>
  <body>
    <a class="skip-link" href="#posts">跳转到文章列表</a>
    <div class="layout">
      <aside class="sidebar">
        <header class="site-header">
          <h1 class="site-title">{SITE_NAME}</h1>
          <p class="site-desc">{html.escape(SITE_HEADER_DESCRIPTION)}</p>
        </header>

{about_html}
      </aside>

      <section class="content">
        <aside class="directory latest-note-panel" id="latest-note-preview">
          <p class="latest-note-title">最近随想</p>
          <p class="latest-note-text" id="latest-note-text">读取中...</p>
          <p class="latest-note-link"><a href="/notes.html">查看全部随想</a></p>
        </aside>

        <aside class="directory" id="posts">
        <nav class="tag-nav" id="tag-nav" aria-label="标签筛选" data-forced-tag="{html.escape(forced_tag)}">
{tag_nav_html}
        </nav>
{main_html}
        </aside>
      </section>
    </div>

    <footer class="site-footer">
      <p>© {datetime.now().year} — {html.escape(SITE_FOOTER_OWNER)}</p>
    </footer>

{json_ld}
{script_line}
  </body>
</html>
"""


def render_about_page(
    site_prefix: str,
    posts: list[dict[str, Any]],
    tag_stats: list[dict[str, Any]],
) -> str:
    canonical_url = join_url(site_prefix, ABOUT_HTML_NAME)
    og_image_url = resolve_cover_url(DEFAULT_COVER_RELATIVE, site_prefix)
    rss_url = join_url(site_prefix, RSS_XML_NAME)
    manifest_url = join_url(site_prefix, MANIFEST_RELATIVE)
    top_tags = tag_stats[:5]
    tag_links = (
        "、".join(
            f'<a href="/index.html?tag={quote(entry["tag"])}">{html.escape(entry["tag"])}</a>'
            for entry in top_tags
        )
        if top_tags
        else "还在慢慢积累中"
    )
    latest_post = posts[0] if posts else None
    about_html = render_sidebar_about_section(
        show_about_link=False,
        extra_links=[f'<a href="/{INDEX_HTML_NAME}">首页</a>', f'<a href="/notes.html">随想</a>'],
        include_rss=True,
    )
    latest_post_html = ""
    if latest_post:
        latest_post_html = (
            f'<p class="about-inline-links">最近一篇文章是 '
            f'<a href="/{html.escape(latest_post["url"])}">{html.escape(latest_post["title"])}</a>'
            f'（{html.escape(latest_post["date"])}）。</p>'
        )

    about_json = f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "AboutPage",
  "name": "关于我 - {SITE_NAME}",
  "url": "{html.escape(canonical_url)}",
  "isPartOf": "{html.escape(join_url(site_prefix, ""))}"
}}
</script>"""

    return f"""<!doctype html>
<html lang="{SITE_LANGUAGE}">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>关于我 - {html.escape(SITE_NAME)}</title>
    <meta name="description" content="{html.escape(SITE_HOME_DESCRIPTION)}" />
    <link rel="canonical" href="{html.escape(canonical_url)}" />
    <link rel="alternate" type="application/rss+xml" title="{SITE_NAME} RSS" href="{html.escape(rss_url)}" />
    <link rel="manifest" href="{html.escape(manifest_url)}" />
    <link rel="icon" href="/assets/img/icon.jpg" type="image/jpeg" />
    <link rel="apple-touch-icon" href="/assets/img/icon.jpg" />
    <meta property="og:type" content="website" />
    <meta property="og:site_name" content="{SITE_NAME}" />
    <meta property="og:title" content="关于我 - {html.escape(SITE_NAME)}" />
    <meta property="og:description" content="{html.escape(SITE_HOME_DESCRIPTION)}" />
    <meta property="og:url" content="{html.escape(canonical_url)}" />
    <meta property="og:image" content="{html.escape(og_image_url)}" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="关于我 - {html.escape(SITE_NAME)}" />
    <meta name="twitter:description" content="{html.escape(SITE_HOME_DESCRIPTION)}" />
    <meta name="twitter:image" content="{html.escape(og_image_url)}" />
    <meta name="theme-color" content="{SITE_THEME_COLOR}" />
    <link rel="stylesheet" href="/assets/css/style.css" />
  </head>
  <body>
    <a class="skip-link" href="#about-main">跳转到个人介绍</a>
    <div class="layout">
      <aside class="sidebar">
        <header class="site-header">
          <h1 class="site-title">{SITE_NAME}</h1>
          <p class="site-desc">{html.escape(SITE_HEADER_DESCRIPTION)}</p>
        </header>

{about_html}
      </aside>

      <section class="content">
        <aside class="directory about-page" id="about-main">
          <h2 class="dir-title">个人介绍</h2>

          <section class="about-block">
            <h3>我是谁</h3>
            <p>虽然其实我也不知道，但是至少必须得写一点，所以我的首页是这样写的：</p>
            <p>{html.escape(ABOUT_LINES[0] if len(ABOUT_LINES) > 0 else "")}</p>
            <p class="about-fact">{html.escape(ABOUT_LINES[1] if len(ABOUT_LINES) > 1 else "")}</p>
            <p class="about-fact">{html.escape(ABOUT_LINES[2] if len(ABOUT_LINES) > 2 else "")}</p>
            <p class="about-fact">{html.escape(ABOUT_LINES[3] if len(ABOUT_LINES) > 3 else "")}</p>
          </section>

          <section class="about-block">
            <h3>这个站里会写什么</h3>
            <p>目前这里有 {len(posts)} 篇文章。内容大多散落在 {tag_links} 这些主题里，虽然其实都是小时候瞎写的，现在一直没时间写也不敢写。</p>
{latest_post_html}
          </section>

          <section class="about-block">
            <h3>怎么逛这个站</h3>
            <p class="about-inline-links"><a href="/{INDEX_HTML_NAME}">首页</a> 适合直接看最新文章，<a href="/notes.html">每天随想</a> 会更短、更即时，<a href="/{RSS_XML_NAME}">RSS</a> 则适合长期订阅。</p>
          </section>

          <section class="about-block">
            <h3>联系我</h3>
            <p class="about-inline-links"><a href="mailto:{html.escape(CONTACT_EMAIL)}">Email</a> · <a href="{html.escape(CONTACT_GITHUB)}">GitHub</a></p>
          </section>
        </aside>
      </section>
    </div>

    <footer class="site-footer">
      <p>© {datetime.now().year} — {html.escape(SITE_FOOTER_OWNER)}</p>
    </footer>

{about_json}
    <script src="/assets/js/site.js" defer></script>
  </body>
</html>
"""


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )


def collect_posts(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    posts_dir = repo_root / POSTS_DIRNAME
    post_files = sorted(posts_dir.rglob("*.html"))
    items: list[dict[str, Any]] = []
    search_items: list[dict[str, Any]] = []

    for post_file in post_files:
        content = read_text(post_file)

        title = get_meta_value(content, "post-title")
        if not title:
            title_match = re.search(r"<title>([^<]+)</title>", content, flags=re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else post_file.stem

        date = get_meta_value(content, "post-date")
        tags_raw = get_meta_value(content, "post-tags")
        excerpt = get_meta_value(content, "post-excerpt")
        cover_raw = get_meta_value(content, "post-cover")

        if not excerpt:
            p_match = re.search(r"<p>([\s\S]*?)</p>", content, flags=re.IGNORECASE)
            excerpt = strip_html(p_match.group(1)) if p_match else ""

        tags = normalize_tags(tags_raw)
        cover = resolve_cover_path(cover_raw, DEFAULT_COVER_RELATIVE)
        relative_post_path = normalize_url_path(str(post_file.relative_to(repo_root)))
        search_content = extract_search_content(content)

        sort_date = parse_date(date) or datetime.min
        item = {
            "id": stable_id(relative_post_path),
            "title": title,
            "date": date,
            "tags": tags,
            "excerpt": excerpt,
            "cover": cover,
            "url": relative_post_path,
            "_sort_date": sort_date,
        }
        items.append(item)
        search_items.append(
            {
                "id": item["id"],
                "title": title,
                "date": date,
                "tags": tags,
                "excerpt": excerpt,
                "cover": cover,
                "url": relative_post_path,
                "content": search_content,
                "_sort_date": sort_date,
            }
        )

    items.sort(key=lambda x: (x["_sort_date"], x["title"]), reverse=True)
    search_items.sort(key=lambda x: (x["_sort_date"], x["title"]), reverse=True)
    for item in items:
        item.pop("_sort_date", None)
    for item in search_items:
        item.pop("_sort_date", None)
    return items, search_items


def build_site(repo_root: Path, site_prefix: str) -> None:
    items, search_items = collect_posts(repo_root)

    index_json_path = repo_root / INDEX_JSON_NAME
    search_json_path = repo_root / SEARCH_JSON_NAME
    index_html_path = repo_root / INDEX_HTML_NAME
    about_html_path = repo_root / ABOUT_HTML_NAME
    rss_path = repo_root / RSS_XML_NAME
    sitemap_path = repo_root / SITEMAP_XML_NAME
    tags_dir = repo_root / TAGS_DIRNAME
    tags_index_path = tags_dir / "index.html"

    write_json(index_json_path, items)
    write_json(search_json_path, search_items)

    tag_map: dict[str, list[dict[str, str]]] = {}
    for post in items:
        for tag in post["tags"]:
            tag_map.setdefault(tag, []).append(
                {
                    "id": post["id"],
                    "title": post["title"],
                    "date": post["date"],
                    "url": post["url"],
                }
            )

    tag_stats: list[dict[str, Any]] = []
    for tag, refs in tag_map.items():
        refs_sorted = sorted(
            refs,
            key=lambda r: parse_date(r["date"]) or datetime.min,
            reverse=True,
        )
        tag_stats.append(
            {
                "tag": tag,
                "count": len(refs_sorted),
                "slug": slugify_tag(tag),
                "path": f"{TAGS_DIRNAME}/{slugify_tag(tag)}.html",
                "posts": refs_sorted,
            }
        )
    tag_stats.sort(key=lambda t: (-t["count"], t["tag"]))

    tag_nav_html = render_tag_nav(items, "")
    home_content = '<h2 class="dir-title">最新文章</h2>\n' + render_post_list(items)
    web_site_json = f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "WebSite",
  "name": "{SITE_NAME}",
  "url": "{html.escape(join_url(site_prefix, ""))}",
  "potentialAction": {{
    "@type": "SearchAction",
    "target": "{html.escape(join_url(site_prefix, "index.html"))}?tag={{tag}}",
    "query-input": "required name=tag"
  }}
}}
</script>"""
    home_html = render_shell(
        site_prefix=site_prefix,
        page_title=SITE_NAME,
        description=SITE_HOME_DESCRIPTION,
        canonical_path=INDEX_HTML_NAME,
        tag_nav_html=tag_nav_html,
        main_html=home_content,
        extra_head="",
        json_ld=web_site_json,
        include_client_script=True,
        forced_tag="",
    )
    write_text(index_html_path, home_html)

    about_html = render_about_page(site_prefix, items, tag_stats)
    write_text(about_html_path, about_html)

    if GENERATE_TAG_PAGES:
        ensure_dir(tags_dir)
        tags_index_list = ['<h2 class="dir-title">标签索引</h2>', '<div class="dir-list">']
        for entry in tag_stats:
            tags_index_list.append('<div class="dir-item">')
            tags_index_list.append(
                f'  <a href="/{html.escape(entry["path"])}">{html.escape(entry["tag"])}</a>'
            )
            tags_index_list.append(
                f'  <span class="meta">{entry["count"]} 篇文章</span>'
            )
            tags_index_list.append("</div>")
        tags_index_list.append("</div>")
        tags_index_html = render_shell(
            site_prefix=site_prefix,
            page_title=f"标签索引 - {SITE_NAME}",
            description="按标签浏览文章。",
            canonical_path=f"{TAGS_DIRNAME}/index.html",
            tag_nav_html=tag_nav_html,
            main_html="\n".join(tags_index_list),
            extra_head="",
            json_ld="",
            include_client_script=False,
            forced_tag="",
        )
        write_text(tags_index_path, tags_index_html)

        by_id = {p["id"]: p for p in items}
        for entry in tag_stats:
            page_tag = entry["tag"]
            page_path = repo_root / entry["path"]
            ensure_dir(page_path.parent)
            current_posts = [by_id[p["id"]] for p in entry["posts"] if p["id"] in by_id]
            tag_page_nav = render_tag_nav(items, page_tag)
            tag_content = (
                f'<h2 class="dir-title">标签：{html.escape(page_tag)}</h2>\n'
                + render_post_list(current_posts)
            )
            tag_page_html = render_shell(
                site_prefix=site_prefix,
                page_title=f"标签 {page_tag} - {SITE_NAME}",
                description=f"标签 '{page_tag}' 下的文章列表。",
                canonical_path=entry["path"],
                tag_nav_html=tag_page_nav,
                main_html=tag_content,
                extra_head="",
                json_ld="",
                include_client_script=False,
                forced_tag=page_tag,
            )
            write_text(page_path, tag_page_html)

    now_utc = datetime.now(timezone.utc)
    channel_link = site_prefix if site_prefix else "/"
    rss_items: list[str] = []
    for item in items:
        link = join_url(site_prefix, item["url"])
        pub_date = to_rfc2822_date(item["date"], now_utc)
        rss_items.append(
            f"""  <item>
    <title>{html.escape(item["title"])}</title>
    <link>{html.escape(link)}</link>
    <guid isPermaLink="true">{html.escape(link)}</guid>
    <pubDate>{pub_date}</pubDate>
    <description>{html.escape(item["excerpt"])}</description>
  </item>"""
        )
    rss_doc = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>{SITE_NAME}</title>
  <link>{html.escape(channel_link)}</link>
  <description>{html.escape(SITE_RSS_DESCRIPTION)}</description>
  <language>{SITE_LANGUAGE}</language>
  <lastBuildDate>{format_datetime(now_utc)}</lastBuildDate>
{chr(10).join(rss_items)}
</channel>
</rss>
"""
    write_text(rss_path, rss_doc)

    sitemap_entries: dict[str, str] = {}
    for rel in SITEMAP_ROOT_PAGES:
        url = join_url(site_prefix, rel)
        sitemap_entries[url] = file_lastmod(repo_root / rel)

    for item in items:
        url = join_url(site_prefix, item["url"])
        sitemap_entries[url] = post_lastmod(item["date"], repo_root / item["url"])

    if INCLUDE_TAG_PAGES_IN_SITEMAP and GENERATE_TAG_PAGES:
        url = join_url(site_prefix, f"{TAGS_DIRNAME}/index.html")
        sitemap_entries[url] = file_lastmod(repo_root / f"{TAGS_DIRNAME}/index.html")
        for entry in tag_stats:
            url = join_url(site_prefix, entry["path"])
            sitemap_entries[url] = file_lastmod(repo_root / entry["path"])

    sitemap_items = [
        f"""  <url>
    <loc>{html.escape(url)}</loc>
    <lastmod>{lastmod}</lastmod>
  </url>"""
        for url, lastmod in sorted(sitemap_entries.items(), key=lambda kv: kv[0])
    ]
    sitemap_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(sitemap_items)}
</urlset>
"""
    write_text(sitemap_path, sitemap_doc)

    print(f"Site URL prefix: {site_prefix if site_prefix else '(relative mode)'}")
    print(f"Generated posts: {len(items)}")
    print(f"Generated tag pages: {len(tag_stats) if GENERATE_TAG_PAGES else 0}")
    print("Updated: index.html, about.html, index.json, search.json, rss.xml, sitemap.xml")


def gather_drafts(repo_root: Path, args: argparse.Namespace) -> list[Path]:
    drafts_dir = repo_root / DRAFTS_DIRNAME
    selected: set[Path] = set()

    for draft in args.draft:
        p = Path(draft)
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        if p.exists() and p.is_file():
            selected.add(p)

    if args.from_drafts:
        ensure_dir(drafts_dir)
        for p in sorted(drafts_dir.glob(args.draft_glob)):
            if p.is_file():
                selected.add(p.resolve())

    return sorted(selected)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build blog: convert markdown drafts and regenerate static pages."
    )
    parser.add_argument(
        "--from-drafts",
        action="store_true",
        help="Convert all markdown files in drafts/ (respecting --draft-glob) before build.",
    )
    parser.add_argument(
        "--draft",
        action="append",
        default=[],
        help="Convert a specific draft markdown file (can be used multiple times).",
    )
    parser.add_argument(
        "--draft-glob",
        default=DEFAULT_DRAFT_GLOB,
        help=f"Glob for drafts when --from-drafts is enabled (default: {DEFAULT_DRAFT_GLOB}).",
    )
    parser.add_argument(
        "--site-url",
        default="",
        help="Override site URL prefix (equivalent to SITE_URL).",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Only convert drafts, do not regenerate site pages.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    site_prefix = resolve_site_prefix(repo_root, args.site_url)
    pandoc_path = find_pandoc()

    drafts = gather_drafts(repo_root, args)
    if drafts:
        if pandoc_path:
            print(f"Using pandoc at: {pandoc_path}")
        else:
            print("Pandoc not found; using fallback markdown converter.")
        for draft in drafts:
            out_path = convert_draft_to_post(draft, repo_root, site_prefix, pandoc_path)
            print(f"Wrote post: {out_path}")
    elif args.from_drafts or args.draft:
        print("No matching drafts found.")

    if not args.skip_build:
        build_site(repo_root, site_prefix)


if __name__ == "__main__":
    main()
