#!/usr/bin/env python3
# 外科式给 personas/*.yaml 加 name_cyr 字段(西里尔文名)。
# 零风险:① 从文件已有的 name_meaning 首个「 · 」前的西里尔词派生(复用文件里已正确的 Cyrillic,
#           不手敲,避开 Latin/Й 混淆);② 纯文本插入到 name: 行之后,不做 YAML 整体 round-trip
#           (不碰 relationships/name_meaning 等其余结构与注释)。幂等:已有 name_cyr 则跳过。
import os, re, glob
D = os.path.join(os.path.dirname(__file__), "..", "personas")
for f in sorted(glob.glob(os.path.join(D, "*.yaml"))):
    txt = open(f, encoding="utf-8").read()
    if re.search(r"^name_cyr:", txt, re.M):
        print("skip (has name_cyr):", os.path.basename(f)); continue
    m = re.search(r"^name_meaning:\s*[\"']?(.*?)[\"']?\s*$", txt, re.M)
    if not m:
        print("!! no name_meaning in", os.path.basename(f)); continue
    leading = m.group(1).split("·")[0].strip()       # 「Зоря · 晨星…」→ Зоря
    leading = leading.split(" ")[0].strip()           # 保险:取首个 token
    # 插到 name: 行之后(lambda 替换避开 re.sub 替换串里 \" 被当字面量的坑)
    txt2 = re.sub(r"(^name:\s*.*$\n)",
                  lambda m: m.group(1) + 'name_cyr: "' + leading + '"\n',
                  txt, count=1, flags=re.M)
    open(f, "w", encoding="utf-8").write(txt2)
    print("added:", os.path.basename(f), "->", leading)
