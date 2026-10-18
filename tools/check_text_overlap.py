#!/usr/bin/env python3
"""在相邻章节之间寻找重复长字面片段；不等于语义去重或抄袭检测。"""
import re
import sys
from pathlib import Path

CHAPTERS = Path(__file__).resolve().parents[1] / "chapters"
WINDOW = 28   # Chinese-character-friendly normalized character window
MIN_MATCH = 36
MAX_HITS = 6

def norm(text):
    # 仅去空白及常用标点。保留数字、汉字、字母。
    return re.sub(r"[\s，。！？；：、‘’“”《》（）()【】\[\],.!?;:\-—…]+", "", text)

def finds(a, b):
    # 把较短章节的 W 字符片段做倒排，不比较所有字符对
    index = {}
    for i in range(0, max(len(a) - WINDOW + 1, 0)):
        index.setdefault(a[i:i+WINDOW], []).append(i)
    hits = []
    seen = set()
    for j in range(0, max(len(b) - WINDOW + 1, 0)):
        key=b[j:j+WINDOW]
        if key not in index: continue
        for i in index[key][:3]:
            # 仅报告最长左边界，减少同一次重复连报
            if i>0 and j>0 and a[i-1]==b[j-1]: continue
            k=WINDOW
            while i+k<len(a) and j+k<len(b) and a[i+k]==b[j+k]: k+=1
            if k<MIN_MATCH: continue
            snippet=a[i:i+min(k,95)]
            if snippet not in seen:
                seen.add(snippet)
                hits.append((k,snippet))
            if len(hits)>=MAX_HITS: break
        if len(hits)>=MAX_HITS: break
    return sorted(hits, reverse=True)

def main():
    chapters=sorted(CHAPTERS.glob("ch[0-9][0-9][0-9]*.md"))
    if len(chapters)<2:
        print("请在 chapters/ 放入至少两个按 ch001.md、ch002.md 命名的定稿章节。")
        return 0
    found=False
    for prev,cur in zip(chapters,chapters[1:]):
        a,b=norm(prev.read_text(encoding="utf-8")),norm(cur.read_text(encoding="utf-8"))
        hits=finds(a,b)
        if hits:
            found=True
            print(f"\n⚠️ 疑似字面重复：{prev.name} vs {cur.name}")
            for length,snippet in hits:
                print(f"  {length}字: {snippet}")
    print("检查完毕：发现疑似重复，请人工判断。" if found else "检查完毕：未发现超过阈值的相邻章节字面重复（不代表剧情无重复）。")
    return 1 if found else 0

if __name__ == "__main__":
    sys.exit(main())
