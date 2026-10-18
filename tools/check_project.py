#!/usr/bin/env python3
"""检查起步项目目录、题材配置、章节编号等基础问题，不审查内容真实性。"""
from __future__ import annotations
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('AGENTS.md','prompt.md','config/novel_profile.md',
            'memory/current_state.md','outline/master_outline.md')
GENRES = {'football','adventure','campus','alternate_history'}

def main() -> int:
    problems=[]
    for rel in REQUIRED:
        if not (ROOT/rel).exists(): problems.append(f'缺失文件: {rel}')
    profile = ROOT/'config/novel_profile.md'
    if profile.exists():
        content=profile.read_text(encoding='utf-8')
        found=re.search(r'^- 当前题材模块：\s*([a-z_]+)', content, re.M)
        if found and found.group(1) in GENRES:
            print('题材模块:', found.group(1))
        else:
            print('提示: 尚未选择有效题材模块（模板允许先不填）。')
    chapters=list((ROOT/'chapters').glob('ch*.md'))
    named=[]
    for p in chapters:
        m=re.fullmatch(r'ch(\d{3,})\.md',p.name)
        if not m: problems.append(f'章节文件命名不规范: {p.name}')
        else: named.append((int(m.group(1)),p))
    nums=sorted(x for x,_ in named)
    if len(nums)!=len(set(nums)): problems.append('章节编号重复（可能是 ch001 与 ch0001 同时存在）')
    if nums and nums!=list(range(1,max(nums)+1)):
        problems.append('章节编号存在空缺，请确认是未定稿还是遗漏')
    print('已定稿章节数:',len(chapters))
    if problems:
        print('\n需要处理：')
        for item in problems: print('-',item)
        return 1
    print('基础结构检查通过（不代表情节或专业事实无误）。')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
