# 一键脚本：填充文章标签（为链接）并生成 index.json / tags.json / rss.xml（PowerShell）
# 用法：在项目根目录运行： pwsh .\scripts\generate_index.ps1
# 说明：本脚本会先把每篇文章 <head> 中的 <meta name="post-tags"> 填入正文（以链接形式指向 /index.html?tag=xxx），然后生成 index.json / tags.json / rss.xml
# 可选：使用 SITE_URL 环境变量或传入参数用于 RSS 中的绝对链接
param(
  [string]$SiteUrl = $env:SITE_URL
)

# Unified step: fill post tags (as links) from head meta before generating index
function Fill-TagsAsLinks {
    Write-Host "Filling post tags as links..." -ForegroundColor Cyan
    $posts = Get-ChildItem -Path (Join-Path $PSScriptRoot '..\posts\*.html') -File
    foreach ($p in $posts) {
        $path = $p.FullName
        $content = Get-Content -Raw -Encoding UTF8 $path

        if ($content -match '<meta\s+name="post-tags"\s+content="([^"]*)">') {
            $tagsRaw = $matches[1]
            $tags = @()
            if ($tagsRaw -ne '') { $tags = ($tagsRaw -split ',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' } }

            $anchors = ($tags | ForEach-Object { '<a href="/index.html?tag=' + [uri]::EscapeDataString($_) + '">' + $_ + '</a>' }) -join ', '

            $pattern = '(<p class="meta">[^<]*?·\s*标签：)\s*[^<]*?(</p>)'
            $newContent = $content -replace $pattern, "`$1$anchors`$2"

            if ($newContent -ne $content) {
                Set-Content -Path $path -Value $newContent -Encoding UTF8
                Write-Host "Updated: $($p.Name) -> $anchors"
            } else {
                Write-Host "No change needed: $($p.Name)"
            }
        } else {
            Write-Host "No tags meta found: $($p.Name)"
        }
    }
}

# Run the tag-fill step (links)
Fill-TagsAsLinks

$posts = Get-ChildItem -Path .\posts -Filter *.html -File -Recurse
$result = @()

foreach($p in $posts){
    $content = Get-Content -Raw -Path $p.FullName

    $title = ([regex]::Match($content,'<meta\s+name="post-title"\s+content="([^"]+)"','IgnoreCase')).Groups[1].Value
    $date  = ([regex]::Match($content,'<meta\s+name="post-date"\s+content="([^"]+)"','IgnoreCase')).Groups[1].Value
    $tagsRaw = ([regex]::Match($content,'<meta\s+name="post-tags"\s+content="([^"]+)"','IgnoreCase')).Groups[1].Value
    $excerpt = ([regex]::Match($content,'<meta\s+name="post-excerpt"\s+content="([^"]+)"','IgnoreCase')).Groups[1].Value

    if(-not $excerpt){
        $m = [regex]::Match($content,'<p>(.+?)</p>', 'Singleline')
        $excerpt = ($m.Success) ? ($m.Groups[1].Value -replace '\s+', ' ' -replace '<[^>]+>','') : ''
    }

    $tags = @()
    if($tagsRaw){ $tags = ($tagsRaw -split ',') | ForEach-Object { $_.Trim() } }

    $obj = [PSCustomObject]@{
        id = [System.Guid]::NewGuid().ToString()
        title = $title
        date = $date
        tags = $tags
        excerpt = $excerpt
        url = "posts/$($p.Name)"
    }
    $result += $obj
}

# 按日期倒序输出更友好
$result = $result | Sort-Object -Property date -Descending

# 输出 JSON（旧的输出被替换为新的综合输出：index.json / tags.json / rss.xml）
# 按日期倒序输出更友好
$items = $result | Sort-Object -Property date -Descending

# 规范化 tags 字段：保证始终为数组（避免字符串被误处理）
foreach($p in $items){
    if(-not ($p.tags -is [System.Array])){
        if([string]::IsNullOrWhiteSpace($p.tags)){
            $p.tags = @()
        } else {
            $p.tags = ($p.tags -split ',') | ForEach-Object { $_.Trim() }
        }
    }
}

# 输出 index.json
$items | ConvertTo-Json -Depth 5 | Set-Content -Path .\index.json -Encoding UTF8
Write-Host "index.json 已更新，包含 $($items.Count) 篇文章。" -ForegroundColor Green

# 生成 tags.json（tag => [{id,title,date,url}, ...]）
$tagMap = @{}
foreach($p in $items){
    foreach($t in ($p.tags | ForEach-Object { $_ })){
        if(-not $tagMap.ContainsKey($t)){
            $tagMap[$t] = @()
        }
        $tagMap[$t] += [PSCustomObject]@{ id=$p.id; title=$p.title; date=$p.date; url=$p.url }
    }
}
$tagsOut = @{}
foreach($k in ($tagMap.Keys | Sort-Object)){
    $tagsOut.$k = @($tagMap[$k] | Sort-Object -Property date -Descending)
}
$tagsOut | ConvertTo-Json -Depth 6 | Set-Content -Path .\tags.json -Encoding UTF8
Write-Host "tags.json 已更新，包含 $($tagsOut.Keys.Count) 个标签。" -ForegroundColor Green

# 生成 RSS（RSS 2.0）
Function EscapeXml([string]$s){ if(-not $s){ return "" } ; return [System.Security.SecurityElement]::Escape($s) }

$sitePrefix = ''
if($SiteUrl){ $sitePrefix = $SiteUrl.TrimEnd('/') } elseif($env:SITE_URL){ $sitePrefix = $env:SITE_URL.TrimEnd('/') }
$nowR = (Get-Date).ToString('R')
$channelLink = if($sitePrefix){ $sitePrefix } else { './' }

$rssItems = ""
foreach($it in $items){
    $link = if($sitePrefix){ "$sitePrefix/$($it.url)" } else { $it.url }
    $pubDate = (Get-Date $it.date).ToString('R')
    $titleEsc = EscapeXml($it.title)
    $descEsc = EscapeXml($it.excerpt)
    $rssItems += "  <item>`n"
    $rssItems += "    <title>$titleEsc</title>`n"
    $rssItems += "    <link>$link</link>`n"
    $rssItems += "    <guid isPermaLink=`"true`">$link</guid>`n"
    $rssItems += "    <pubDate>$pubDate</pubDate>`n"
    if($descEsc){ $rssItems += "    <description>$descEsc</description>`n" }
    $rssItems += "  </item>`n"
}

$rss = @"
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>古早风技术笔记</title>
  <link>$channelLink</link>
  <description>静态 HTML 技术笔记 - 自动生成的 RSS</description>
  <language>zh-CN</language>
  <lastBuildDate>$nowR</lastBuildDate>
$rssItems</channel>
</rss>
"@

Set-Content -Path .\rss.xml -Value $rss -Encoding UTF8
Write-Host "rss.xml 已生成（$($items.Count) 条）。" -ForegroundColor Green
