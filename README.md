# VibeNovel Starter v2 — AI 长篇小说写作模板

这是一套可编辑的本地 **Markdown + Python** 项目，适合用 VS Code / Codex / Obsidian 协作写长篇小说，不依赖付费数据库，不是独立 GUI 软件。

## 更新内容
在原版 27 个文件基础上新增：足球竞技、悬疑探险、青春校园、历史架空四套题材约束，题材素材来源导航，独立的连续性/反重复/审稿提示词，题材专属表单，上下文打包脚本与项目结构检查。

## 五分钟开始
1. 解压后在 VS Code 或 Codex 打开 **整个项目文件夹**；确认工具能读取根目录 `AGENTS.md`。
2. 填写 `config/novel_profile.md`，特别是「当前题材模块」（`football` / `adventure` / `campus` / `alternate_history`）。
3. 填写 `bible/` 下角色、世界、地点、物品、规则；只记已经确定的事实。
4. 填写 `outline/master_outline.md`、`outline/volume_01.md`，复制 `templates/chapter_plan.md` 到 `outline/chapter_plans/ch001.md`，细化第一章计划。
5. 给 Codex 的首句：**“按照 AGENTS.md，读取项目设定和题材模块，先审核第一章计划，不要直接写正文。”**
6. 作者批准计划后写 `drafts/ch001.md`；审查通过并由作者确认后保存为 `chapters/ch001.md`。
7. 定稿后更新 `memory/current_state.md`、`chapter_summaries.md`、`event_ledger.md`、`timeline.md`、`open_threads.md`、`repetition_ledger.md`。下一章再循环。

## 核心原则
- 已定稿正文 + 已确认设定属于小说内部事实；外部小说、考古/体育/历史资料只作参考。
- **禁止无意义重复 ≠ 禁止重提角色或道具**。角色必须延续，伏笔应当回响；关键在于每次重现有没有推进。
- 草稿不能污染正式记忆。任何关键状态修改须可追溯至具体章节。
- 每次加载当前章必要上下文，过往细节不足时再向历史章节追溯，避免全书硬塞入上下文窗口。
- 题材模块负责领域逻辑；通用提示词负责叙事质量、连续性和反重复。

## 目录
- `AGENTS.md`：Codex 等编码 Agent 的入口指导；并非所有聊天模型都会自动读取，未自动读取时请显式要求。
- `prompt.md`：通用小说写作规定。
- `prompts/`：写作、连续性、反重复、审稿分工及四套题材规则。
- `config/`：小说定位和题材选择。
- `bible/`：**固定设定**（角色、世界、地点、物品、规则）。
- `outline/`：总纲、卷纲、每章计划。
- `memory/`：**随定稿演化**的当前状态、事件、伏笔、时间线等。
- `drafts/`、`chapters/`：草稿与最终正文，严禁自动混淆。
- `templates/`：章纲、审稿与题材专属记录模板。
- `references/`：书目、来源、写作技法与四类题材资源导航。
- `tools/`：简单命令行辅助脚本，需 Python 3.9+，仅使用标准库。

## 命令（在项目根目录）
```bash
python tools/check_project.py
python tools/build_context.py 1 --output context_ch001.md
python tools/check_text_overlap.py
```
`build_context.py` 默认输出到终端；`--output` 参数可写入指定的 UTF-8 文件。生成的 `context_ch001.md` 是暂存资料，不是定稿事实源。

## 一套可直接复制给 Codex 的工作流指令
> 按照 AGENTS.md 与当前题材模块，阅读已确认设定和记忆，检查 `outline/chapter_plans/ch001.md` 是否与过去情节矛盾。先输出章节衔接、事件推进与反重复分析，不要立即写正文。等我确认后写草稿，并独立审稿；只有我确认定稿以后，才更新正式章节和记忆。

## 重要限制
- 脚本只做基础辅助；它们**不能**证明剧情零矛盾、零重复或符合投稿平台政策。
- 商业网文可供合法阅读、研究结构和做个人笔记，不代表可以批量采集或再分发受版权保护的全文。
- 官方资料也可能更新，真实足球规则、历史考证与公开资源许可需要在写作和发表前重新核实。
