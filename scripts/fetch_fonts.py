#!/usr/bin/env python3
# 拉取 Google Fonts CSS2(Chrome UA → woff2),保留 unicode-range 子集化,
# 下载到 frontend/fonts/ 并生成 frontend/fonts.css。可复用:改 FAMILIES 重跑即可。
#
# 用法:.venv/bin/python scripts/fetch_fonts.py
import os, re, sys, urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
ROOT = os.path.join(os.path.dirname(__file__), "..", "frontend")
OUT = os.path.join(ROOT, "fonts")
os.makedirs(OUT, exist_ok=True)

# family -> [weights];Cormorant Garamond(衬线 display,带 Cyrillic)+ Hanken Grotesk(正文 sans)
FAMILIES = {
    "Cormorant+Garamond": [500, 600],
    "Hanken+Grotesk": [400, 500, 600],
}
# 简短本地族名 → CSS family 别名
ALIAS = {"Cormorant+Garamond": "Cormorant Garamond", "Hanken+Grotesk": "Hanken Grotesk"}

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        f.write(r.read())
    return os.path.getsize(path)

# 匹配单个 @font-face 块
FACE_RE = re.compile(r"@font-face\s*\{([^}]*)\}", re.S)

css_out = []
total = 0
for fam, weights in FAMILIES.items():
    w = ";".join(str(x) for x in weights)
    css_url = (f"https://fonts.googleapis.com/css2?family={fam}:wght@{w}"
               "&display=swap")
    print(f"fetching CSS: {css_url}")
    css = fetch(css_url)
    for m in FACE_RE.finditer(css):
        body = m.group(1)
        # 只取 woff2(src 里可能多个,挑 .woff2)
        src_match = re.search(r"src:\s*url\((https://[^)]+\.woff2)\)", body)
        if not src_match:
            continue
        url = src_match.group(1)
        weight = re.search(r"font-weight:\s*(\d+)", body)
        style = re.search(r"font-style:\s*(\w+)", body)
        urange = re.search(r"unicode-range:\s*([^;]+);", body)
        weight_v = weight.group(1) if weight else "400"
        style_v = style.group(1) if style else "normal"
        urange_v = urange.group(1).strip() if urange else ""
        # 本地文件名:用 gstatic URL 完整 basename(每文件内容哈希唯一,不撞名)
        base = url.rsplit("/", 1)[-1]          # 形如 <hash>.woff2,全局唯一
        fname = f"{fam.replace('+','')}-{weight_v}-{style_v}-{base}"
        path = os.path.join(OUT, fname)
        sz = download(url, path)
        total += sz
        print(f"  {ALIAS[fam]} w{weight_v} {style_v:6s} {urange_v[:30]:30s} -> {fname} ({sz//1024}KB)")
        lines = [
            "@font-face {",
            f"  font-family: '{ALIAS[fam]}';",
            f"  font-style: {style_v};",
            f"  font-weight: {weight_v};",
            "  font-display: swap;",
            f"  src: url('/static/fonts/{fname}') format('woff2');",
        ]
        if urange_v:
            lines.append(f"  unicode-range: {urange_v};")
        lines.append("}")
        css_out.append("\n".join(lines))

with open(os.path.join(ROOT, "fonts.css"), "w", encoding="utf-8") as f:
    f.write("/* Dawn Observatory 字体:Cormorant Garamond(display 衬线,带 Cyrillic)"
            " + Hanken Grotesk(正文 sans)。由 scripts/fetch_fonts.py 生成。*/\n")
    f.write("\n".join(css_out))

print(f"\nDONE. {len(css_out)} files, {total//1024}KB total -> frontend/fonts/ + frontend/fonts.css")
