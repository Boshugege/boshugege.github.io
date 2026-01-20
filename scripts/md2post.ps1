<#
Convert Markdown (with YAML front matter) or Typst (if requested) to a post HTML file in `posts/`.
Usage:
  pwsh -NoProfile -File .\scripts\md2post.ps1 -Input drafts/sample.md

Features:
- Prefer using `pandoc` when available (supports math via KaTeX/MathJax and syntax highlighting).
- If `pandoc` is not available, uses a conservative fallback converter that preserves:
  - paragraphs, headings, inline code, fenced code blocks, links
  - inline math ($...$) and display math ($$...$$) are preserved for MathJax rendering
- Generates a post HTML file with required meta tags (`post-title`, `post-date`, `post-tags`, `post-excerpt`).
- Adds MathJax and Highlight.js to post head so math and code render correctly.
#>

param(
    [Parameter(Mandatory=$true)][string]$Source
)

Set-StrictMode -Version Latest

function Ensure-Dir([string]$p){ if(-not (Test-Path $p)){ New-Item -ItemType Directory -Path $p | Out-Null } }

$scriptDir = $PSScriptRoot
Ensure-Dir (Join-Path $scriptDir '..\posts')

if(-not (Test-Path $Source)){
    Write-Error "Input file not found: $Source"; exit 1
}

# Read source
$raw = Get-Content -Raw -Encoding UTF8 $Source

# Parse YAML front matter (--- ... ---)
$meta = @{}
if($raw -match '^-{3}\s*([\s\S]*?)\s*-{3}\s*'){
    $yaml = $matches[1]
    $rest = $raw.Substring($matches[0].Length)
    foreach($line in $yaml -split "`n"){
        $l = $line.Trim()
        if($l -match '^([A-Za-z0-9_-]+)\s*:\s*(.*)$'){
            $k = $matches[1]; $v = $matches[2].Trim()
            if($v.StartsWith('"') -and $v.EndsWith('"')){ $v = $v.Substring(1, $v.Length-2) }
            if($v.StartsWith("'") -and $v.EndsWith("'")){ $v = $v.Substring(1, $v.Length-2) }
            $meta[$k] = $v
        }
    }
} else {
    $rest = $raw
}

# helper: slug from title or filename
function Slugify([string]$s){
    if(-not $s){ return [io.path]::GetFileNameWithoutExtension($Source) }
    $out = $s.ToLower()
    # allow unicode letters and numbers (keep Chinese characters too), remove only punctuation except - and _ and spaces
    $out = $out -replace "[^\p{L}\p{N}\s-_]",""
    $out = $out -replace "[\s_]+","-"
    $out = $out -replace "^-+|-+$",""
    if([string]::IsNullOrWhiteSpace($out)){
        return [io.path]::GetFileNameWithoutExtension($Source)
    }
    return $out
}

$title = $meta.title ? $meta.title : ([io.path]::GetFileNameWithoutExtension($Source))
$date = $meta.date ? $meta.date : (Get-Date -Format yyyy-MM-dd)
$tags = $meta.tags ? $meta.tags : ""
$excerpt = $meta.excerpt ? $meta.excerpt : ""

# Determine slug: allow explicit YAML `slug` override, otherwise slugify title
if ($meta.ContainsKey('slug') -and -not [string]::IsNullOrWhiteSpace($meta['slug'])) {
    $slugSource = $meta['slug']
} else {
    # Use source filename (without extension) as default slug to ensure predictable filenames
    $slugSource = [io.path]::GetFileNameWithoutExtension($Source)
}
$slug = Slugify $slugSource

# Build output filename as: YYYY-MM-DD-<slug>.html
$prefixDate = (Get-Date $date -ErrorAction SilentlyContinue).ToString('yyyy-MM-dd')
if(-not $prefixDate){ $prefixDate = (Get-Date).ToString('yyyy-MM-dd') }
$outFilename = "$prefixDate-$slug.html"
$outPath = Join-Path (Join-Path $scriptDir '..\posts') $outFilename

# Try using pandoc if available (detect common install locations and PANDOC_PATH env var)
$pandocCmd = Get-Command pandoc -ErrorAction SilentlyContinue
$pandocExe = $null
if ($pandocCmd) {
    $pandocExe = $pandocCmd.Source
} elseif ($env:PANDOC_PATH -and (Test-Path $env:PANDOC_PATH)) {
    $pandocExe = $env:PANDOC_PATH
} elseif ($env:LOCALAPPDATA) {
    $tryPath = Join-Path $env:LOCALAPPDATA 'Pandoc\pandoc.exe'
    if (Test-Path $tryPath) { $pandocExe = $tryPath }
}

if ($pandocExe) {
    Write-Host "Using pandoc at: $pandocExe" -ForegroundColor Green
    $pandocArgs = @(
        '--from','markdown+yaml_metadata_block+tex_math_dollars',
        '--mathjax',
        '--syntax-highlighting','tango'
    )
    $tmpHtml = [IO.Path]::GetTempFileName()
    & $pandocExe @pandocArgs -o $tmpHtml -- $Source
    $rawBody = Get-Content -Raw -Encoding UTF8 $tmpHtml
    Remove-Item $tmpHtml -Force

    # If pandoc returned a full HTML document (unexpected), extract the content inside <body> to avoid nested docs
    if ($rawBody -match '(?s)<body[^>]*>(.*?)</body>') {
        $body = $matches[1]
    } else {
        $body = $rawBody
    }
} else {
    Write-Host "Pandoc not found; using fallback converter (conservative)." -ForegroundColor Yellow

    # Minimal fallback conversion: handle code fences, inline code, headings, paragraphs, links, images
    $text = $rest -replace "\r\n","`n"

    # Convert fenced code blocks ```lang ... ```
    $text = [regex]::Replace($text, '```([a-zA-Z0-9_-]*)\n([\s\S]*?)\n```', { param($m) 
        $lang = $m.Groups[1].Value; $code = $m.Groups[2].Value;
        $escaped = [System.Net.WebUtility]::HtmlEncode($code)
        if($lang -ne ''){ return '<pre><code class="language-' + $lang + '">' + $escaped + '</code></pre>' } else { return '<pre><code>' + $escaped + '</code></pre>' }
    }, 'Singleline')

    # Inline code `...`
    $text = [regex]::Replace($text, '`([^`]+)`', { param($m) "<code>" + [System.Net.WebUtility]::HtmlEncode($m.Groups[1].Value) + "</code>" })

    # Headings: ### to h3
    $text = [regex]::Replace($text, '^###\s*(.+)$','<h3>$1</h3>','Multiline')
    $text = [regex]::Replace($text, '^##\s*(.+)$','<h2>$1</h2>','Multiline')
    $text = [regex]::Replace($text, '^#\s*(.+)$','<h1>$1</h1>','Multiline')

    # Links [text](url)
    $text = [regex]::Replace($text, '\[([^\]]+)\]\(([^\)]+)\)', '<a href="$2">$1</a>')

    # Paragraphs: split by blank lines
    $paras = $text -split "`n{2,}"
    $outHtml = ""
    foreach($p in $paras){
        $p = $p.Trim()
        # Preserve display math $$...$$ blocks as-is (do not wrap or replace line breaks)
        if($p -match '^[\$]{2}[\s\S]*[\$]{2}$'){
            $outHtml += $p + "`n"
        } elseif($p -match '^<h' -or $p -match '^<pre>' -or $p -match '^<'){
            $outHtml += $p + "`n"
        } else {
            $line = $p -replace "`n","<br/>"
            $outHtml += "<p>$line</p>`n"
        }
    }

    # Preserve inline math $...$ as-is (MathJax will render)
    $body = $outHtml
}

# Build final HTML with head meta and required scripts for math and highlight
$headExtras = @'
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/styles/default.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/highlight.min.js" defer></script>
  <script>
    document.addEventListener('DOMContentLoaded',()=>{ document.querySelectorAll('pre code').forEach((el)=> { if(window.hljs) hljs.highlightElement(el); }); });
  </script>

  <script>
    window.MathJax = { tex: { inlineMath: [['$','$'], ['\\(','\\)']] } };
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
        if (!lang){ const m = pre.innerHTML.match(/class=\"[^\"]*language-([a-zA-Z0-9]+)[^\"]*\"/); if(m) lang = m[1]; }

        if (lang) {
          const normalized = lang.toLowerCase();
          // only show if language was actually specified (we detect common patterns above)
          const display = normalized.replace(/^[a-z]/, (s) => s.toUpperCase());
          pre.setAttribute('data-lang', display);
        }
      });
    });
  </script>
'@

# Build meta line for body (date + tags links, consistent with existing posts)
$anchors = ""
if(-not [string]::IsNullOrWhiteSpace($tags)){
    $tagsArr = ($tags -split ',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
    if($tagsArr.Count -gt 0){
        $anchors = ($tagsArr | ForEach-Object { '<a href="/index.html?tag=' + [uri]::EscapeDataString($_) + '">' + [System.Net.WebUtility]::HtmlEncode($_) + '</a>' }) -join ', '
    }
}
$metaLine = '    <p class="meta">' + $date + ' · 标签：' + $anchors + '</p>' + "`n"

# Prepend meta line to body so it matches existing post style
if(-not [string]::IsNullOrWhiteSpace($metaLine)){
    $body = $metaLine + $body
}

# Compose final document
$doc = @"
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>$title</title>
  <meta name="post-title" content="$([System.Net.WebUtility]::HtmlEncode($title))">
  <meta name="post-date" content="$date">
  <meta name="post-tags" content="$([System.Net.WebUtility]::HtmlEncode($tags))">
  <meta name="post-excerpt" content="$([System.Net.WebUtility]::HtmlEncode($excerpt))">
  <link rel="stylesheet" href="/assets/css/style.css">
$headExtras
</head>
<body>
  <article class="post-content">
    <h1>$title</h1>
$body
  </article>
</body>
</html>
"@

# Write out
Set-Content -Path $outPath -Value $doc -Encoding UTF8
Write-Host "Wrote post: $outPath" -ForegroundColor Green

# Optionally: run generate_index to refresh site metadata
if((Get-Command pwsh -ErrorAction SilentlyContinue)){
    Write-Host "Refreshing index/tags/rss by running generate_index.ps1" -ForegroundColor Cyan
    & (Join-Path $scriptDir 'generate_index.ps1')
}
