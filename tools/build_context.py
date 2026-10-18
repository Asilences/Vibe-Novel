#!/usr/bin/env python3
"""只读拼接章节创作上下文。不能替代检索或自动判定事实。"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
GENRES = {'football','adventure','campus','alternate_history'}

def read(rel: str) -> str:
    p = ROOT / rel
    if not p.is_file():
        return f'[尚无文件：{rel}]'
    return p.read_text(encoding='utf-8')

def main() -> int:
    parser = argparse.ArgumentParser(description='生成单章上下文包，供 AI 创作前审核')
    parser.add_argument('chapter', type=int, help='章节号，例如 1 或 12')
    parser.add_argument('--genre', choices=sorted(GENRES), help='指定题材；不指定则读取 config/novel_profile.md')
    parser.add_argument('--output', help='写入路径；不填则打印到终端')
    args = parser.parse_args()
    if args.chapter < 1:
        parser.error('章节号至少为 1')
    profile = read('config/novel_profile.md')
    m = re.search(r'^- 当前题材模块：\s*([a-z_]+)', profile, flags=re.M)
    genre = args.genre or (m.group(1) if m else None)
    files = [
        'AGENTS.md','prompt.md','config/novel_profile.md',
        'prompts/chapter_writer.md','prompts/continuity.md','prompts/repetition.md',
        'outline/master_outline.md', 'outline/volume_01.md',
        f'outline/chapter_plans/ch{args.chapter:03d}.md',
        'memory/current_state.md', 'memory/chapter_summaries.md',
        'memory/open_threads.md','memory/repetition_ledger.md',
        'memory/event_ledger.md', 'memory/timeline.md',
    ]
    if genre in GENRES:
        files.insert(3, f'prompts/genres/{genre}.md')
    else:
        print('注意：没有识别到有效题材模块；可用 --genre 明确指定。', file=sys.stderr)
    if args.chapter > 1:
        files.append(f'chapters/ch{args.chapter - 1:03d}.md')
    sections = [f'# 第 {args.chapter:03d} 章写作上下文（自动生成，仅供阅读，不是权威事实）']
    for rel in files:
        sections += [f'\n\n---\n\n## 文件：{rel}\n', read(rel)]
    sections.append('\n\n---\n\n> 注意：包内没有自动补充角色/地点的全部历史章节。若有关联事实或冲突，仍需按事件ID和章节原文检索核验。\n')
    content='\n'.join(sections)
    if args.output:
        p = Path(args.output)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        print(f'已生成上下文包：{p}，{len(content)} 字符')
    else:
        sys.stdout.write(content)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
