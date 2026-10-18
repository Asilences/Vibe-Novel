# VibeNovel v3 — 高自主 AI 长篇小说创作系统详细功能设计

> 设计稿 v3.0 · 2026-10-18 · 状态：待实现 · 默认模式：高自主策划、重大变更人工审批
>
> v2 目前只是 Markdown + Python 辅助模板。本文规定 v3 的目标行为、数据契约、技术架构、验收标准，不声称这些功能已经实现。

## 0. 一句话定位

面向单作者、多小说、多题材的本地优先 AI 写作工作台：支持导入已有大纲或从创意启动；自动诊断和分层扩写；规划和续写长篇正文；使用结构化情节记忆检查连续性与重复；支持 Codex 交互执行或通过 DeepSeek/MiMo 等外部模型 API 独立运行；**AI 有提案权，作者掌握重大改编的最终批准权**。

### 非目标（v3 第一阶段不做）
- 不承诺文本“零重复”“永不遗忘”或任何平台签约收益。
- 不未经确认发布作品、不批量爬取有版权限制的小说全文。
- 不把一大堆不同职责的 Agent 必须绑定为不同收费模型；可用同一模型扮演不同逻辑角色。
- 不强依赖 Web 界面、在线向量数据库或云端常驻服务才能运行。

## 1. 角色与权限矩阵

| 级别 | 示例 | 默认自动化规则 | 影响对象 |
|---|---|---|---|
| L1 微观写作 | 动作、对白、景物、心理、常规无名配角 | 可自动提出和写入草稿；禁止突破既有事实 | draft |
| L2 一般情节 | 次要支线、节奏调整、新线索、配角关系小变化 | 当前卷内、未触碰锁定节点/定稿事实、在已授权范围内可自动写入候选计划；作者可收紧为全部审批 | outline proposal / draft |
| L3 重大改编 | 主线终点、主角命运、关键生死、核心世界规则、主要人物关系身份反转、跨卷结构、修改定稿事件 | **必须显式作者批准，未批前任何正式设定/已批准大纲/定稿正文不得改变** | canon / approved outline / published chapters |

额外触发 L3：触碰作者 `locked` 的大纲事件；修改已经定稿的历史事实；导致跨卷连锁改动；与原定结局不一致；改变题材承诺；删除重要伏笔而不替代；小说原始大纲中的不可改动条款。

**审批单 ChangeRequest**：`id`、`novel_id`、`kind`、`level`、`base_revision`、`proposed_diff`、`rationale`、`alternatives`、`impact_files`、`affected_chapters`、`impact_on_foreshadowing`、`risk`、`status`、`author_decision`、`decision_at`、`applied_revision`。

状态机：`PROPOSED -> PENDING_APPROVAL -> APPROVED | REJECTED | REVISION_REQUESTED -> APPLIED`；如果基础版本变化则转 `STALE`，必须重新计算影响。`APPROVED` 不等于立即写入正文；只有经过版本校验的 Apply 操作才能执行。

**审批安全边界**：API 工作流中模型只能输出候选操作，由后端策略引擎校验；实际批准事件只由人类操作创建。Codex 直接模式下，项目指令不能构成强制安全隔离，应仅在草稿/提案分支工作；正式主分支的合并和重大变更仍由人类执行。具备同一操作系统写权限的代理原则上可以绕过提示词，因此强防护需借助独立审批进程、权限或受保护远程仓库。

## 2. 双启动入口与大纲管理

### 2.1 新小说入口
- **大纲导入**：`.md` / `.txt` / `.docx` / 文本可提取的 `.pdf`；扫描 PDF 的 OCR 属可选扩展，不默认依赖。
- **创意创建**：输入题材、主角、核心冲突、篇幅、情感基调，AI 提交不少于 2 套方向不同的总纲供作者选定。
- 导入原件只读保存 `imports/original/`，记录 SHA256、原始文件名、提取时间及页/段落来源。

### 2.2 智能解析与诊断
提取：世界观、背景时间、主题、主要人物、人物关系、主线/支线、节点、分卷、事件先后、伏笔、结局、锁定约束。

输出：
1. **事实/假设分离**：标注 `SOURCE`、`AI_SUGGESTED`、`AUTHOR_APPROVED`，不能把 AI 推断伪装成原稿事实。
2. **大纲缺陷报告**：动机不足、逻辑冲突、节奏拥堵、角色失衡、空白转折、没回收的伏笔、跨章重复风险。
3. **可选改编路线**：保守、均衡、激进三案，分别列收益、代价、改变范围；未经批准不实施 L3。
4. **锁定表**：作者可锁定主角性格底色、命运、结局、关键章节事件、世界观公理。

### 2.3 大纲粒度与版本
`Book > Arc > Volume > Chapter > Scene Beat`，每一级包括 `id`、`goal`、`conflict`、`turn`、`outcome`、`characters`、`timeline`、`dependencies`、`source_refs`、`locked_fields`、`version`。

- 总纲扩写为卷纲；卷纲拆成章节计划；章节计划再拆场景节拍。
- 重大改编提交 ChangeRequest，在视觉/文本 diff 中比较 `原文 / 建议 / 采纳`。
- 作者锁定章节或节点后，计划生成器不再直接覆盖，必须另行提案。
- 新版本出现时，仅对 **未定稿** 的后续计划生成更新候选，不自动重写历史正文。

## 3. 多 Agent 逻辑角色与工作流

> Agent = 一组任务职责 + Prompt + 输入输出 Schema，不要求每个逻辑 Agent 启动独立进程或独立付费模型。

| 角色 | 输入 | 输出 | 是否可改动正式文件 |
|---|---|---|---|
| OutlineAnalyst（大纲分析） | 原纲、锁定规则 | 大纲结构、缺陷、来源证据 | 否 |
| PlotArchitect（剧情策划） | 大纲、历史事件、伏笔 | 章节/场景候选、改编提案 | 否 |
| ContinuityKeeper（连续性） | 近期正文、人物状态、检索历史 | 时空/知识/物品/时间冲突报告 | 否 |
| ChapterWriter（正文写手） | 已批准计划、上下文包、题材规范 | 可继续编辑的章节草稿 | 仅 drafts |
| LiteraryEditor（文学编辑） | 草稿、风格档案、历史节奏 | 叙事、人物、文风、语义重复报告 | 仅 revision proposal |
| MemorySteward（记忆维护） | 定稿正文、历史状态、引用位置 | 待提交的状态增量、伏笔增量 | 无直接审批权 |

可将前三个逻辑角色顺序执行，减少模型调用；对于重大剧情冲突，在不同角色/不同模型间交叉复核。

**标准章节管线**

```
load approved outline + recent context + relevant memory
       ↓
scene plan and L3 change scan ── L3? → approval queue / STOP
       ↓
create draft (one scene at a time, with scene checkpoints)
       ↓
deterministic checks → continuity review → semantic repetition review → literary edit
       ↓
revision proposal + draft candidate
       ↓
author finalization
       ↓
atomic commit: chapters + event ledger + state snapshot + clues + summary
       ↓
Git snapshot / output hash / next chapter readiness
```

**作者确认的粒度**：项目初始策划、每卷总纲、所有 L3 提案及正式定稿必须批准。v3 可配置“连续生成最多 N 章草稿，但不得越过待审批结构改编或定稿边界”。

## 4. 长篇小说记忆系统

| 层级 | 内容 | 读取方式 | 可修改时机 |
|---|---|---|---|
| 固定设定 Canon | 确认世界观、主角初始身份、世界规则、锁定节点 | ID/关键词精确查找 | 作者批准 |
| 剧情事件 Event Ledger | 发生时间、角色、因果、证据章节、事件重要性 | ID/时间/角色查找 | 定稿事务 |
| 当前状态 State Snapshot | 角色地点、伤势、物品、知情信息、关系、队伍、资源 | 最新有效快照 | 定稿事务 |
| 章节记忆 Summaries | 每章变更、揭示、冲突、悬念与承接点 | 近期窗口+历史关键词 | 定稿事务 |
| 伏笔与谜题 Threads | 提出、提示、误导、推进、回收、遗留 | 状态/角色/期限查询 | 定稿事务 |
| 作品全文 Archive | 最终正文、段落锚点、历史版本 | 定向回查 | 作者定稿 |
| RAG Index（后期） | 章节/人物/事件语义索引 | 混合检索 | 随定稿重建 |

上下文组装顺序：当前项目约束 → 当前已批准计划 → 最新角色/时间状态 → 上一章完整结尾（必要时全文）→ 最近 3~5 章摘要 → 按实体、事件和伏笔 ID 提取过去证据。对存在冲突的多份资料，不作无依据综合，明确引用原章节并报告。

**禁止草稿污染**：所有从草稿提取的“事实”标为 `provisional`，不进入正式事件表、快照和 RAG 的 canonical collection；只有章节定稿后经状态增量校验才能升级。

**事件溯源**：每个重要事实具有 `entity_id`、`event_id`、`source_chapter`、`source_span`、`valid_from` 和版本信息。人物可能故意撒谎或不知道某事，因此同时区分“客观剧情事实”和“角色所相信的事情”。

## 5. 反重复与质量控制

三层审查：
1. **字面**：重复 n-gram、相似段落、固定套话、相邻章节反复回顾。只生成风险候选，不机械删除必要回忆。
2. **事件/机制**：事件签名（目标、障碍、角色应对、代价、结局），识别只是换皮的情节循环。允许主题呼应、重复母题和升级重演。
3. **叙事**：人物成长、因果、节奏、对白、信息密度、伏笔公平性、场景推进。输出证据与建议，而非只给无依据分数。

审核报告记录严重度 `BLOCKER / MAJOR / MINOR / INFO`、章节依据、可复现检查与建议。不把模型的主观评价当成数学上的硬性事实。

质量门禁：结构冲突=BLOCKER；已确认事实的明确矛盾=BLOCKER；疑似情节雷同=MAJOR 待作者复核；文风问题=MINOR。对于复杂悬疑的故意误导，采用标注后豁免机制。

## 6. 四题材专业约束

- **football**：比赛日历、球员合同/伤停、阵容、比赛事件与比分、积分榜、转会账、战术与体能状态；模拟器先定赛事事实，正文后写。
- **adventure**：遗迹地图拓扑、队员位置、线索所有权/知情者、谜题解答约束、携带资源、失踪/伤势；尊重虚构与真实考古区分。
- **campus**：学年学期、校历、考试、课表、年龄、校园人际图、情绪节点、关系变化；检查成长阶段和时间跳跃。
- **alternate_history**：真实历史基线 + 小说分歧时间线；政权、行政、官职、兵力、粮草、交通、技术发展、人口与资源约束。

每部小说声明一个主类型与多个副类型；模块可以组合，但若同一事实由两个模块定义必须指定权威来源，不得随意覆盖。

## 7. 双运行模式 + API 网关

### 7.1 共用部分

`NovelCore`：项目仓库、任务与状态机、上下文组装、审批单、事件日志、质量校验、导出；这部分完全与具体模型解耦。

- `CodexAdapter`：Codex 在项目工作目录执行对应提示词；读取 `AGENTS.md`。不会把 Codex 的会话额度视为 DeepSeek/MiMo API 余额，也不要求 v3 另备 API key 才能以交互形式工作。
- `APIAdapter`：Python 调用兼容 OpenAI Chat Completions 的模型提供方，可根据不同任务路由，并标准化错误、usage、流式与 JSON 解析。
- `ModelCapabilities`：登记是否支持结构化输出、tools、长上下文、流式、指定 temperature 等；不支持的参数自动禁用并明确日志。

### 7.2 建议配置示例

```yaml
# config/models.yaml — 示例配置，需联网和密钥才能真实调用
execution:
  mode: api          # api | codex_interactive
  approval_policy: human_for_L3
  default_max_draft_chapters: 1

providers:
  deepseek:
    protocol: openai_chat
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    models:
      flash: deepseek-flash
  mimo:
    protocol: openai_chat
    base_url: https://api.xiaomimimo.com/v1
    api_key_env: MIMO_API_KEY
    models:
      flash: mimo-v2.6-flash
      pro: mimo-v2.6-pro

roles:
  outline_architect: mimo.pro
  chapter_writer: deepseek.flash
  consistency_review: mimo.flash
  memory_extract: deepseek.flash

limits:
  timeout_seconds: 180
  max_retries: 2
  max_chapter_hanzi: 3800
  daily_spend_ceiling: null   # null 表示尚未设置预算，不代表免费
```

`roles` 是初始推荐的可配置映射，**不是模型文学创作能力排名**。温度、上下文长度和输出限制从 provider capability/cost 表读取；不能假定所有模型在思考模式下都接受温度设置。Key 只通过环境变量、系统密钥存储等获取，绝不写入 Git 或运行日志。

Codex 直接模式建议提供复制可用的命令入口（例如 `/plan-arc`、`/review-chapter` 对应 prompts）；如需自动命令行编排，应另做 `codex exec` 适配器并在目标计算机检查版本和授权；不把 Codex 当作未经验证的通用 OpenAI Chat Completion HTTP 接口。

### 7.3 稳定性/可观测性
- `task_id` + `chapter_id` + `base_revision` 幂等写入。重试不会产生两个正式章节。
- 流式输出先写 `tmp/`，完成后校验字数、Schema 和完成状态，再保存草稿。
- 429/5xx 指数退避；401/403、余额不足、不支持参数、被作者拒绝等明确分类，不盲目重试。
- 记录请求时间、token、模型 ID、估算费用、任务成功率、字数和错误类型；不记录明文 API key。
- 人类改稿也应触发对本章状态增量的重新提取和检查。

## 8. 项目文件结构（建议）

```text
VibeNovel-v3/
├── AGENTS.md                 # Codex 入口，要求遵守规划/审批边界
├── README.md
├── prompts/
│   ├── outline_analysis.md
│   ├── plot_architect.md
│   ├── chapter_writer.md
│   ├── memory_extract.md
│   ├── review_continuity.md
│   ├── review_repetition.md
│   └── genres/{football,adventure,campus,alternate_history}.md
├── config/
│   ├── models.example.yaml
│   ├── app.yaml
│   └── .env.example
├── src/vibenovel/
│   ├── cli.py
│   ├── core/{project,workflow,approval,context,commit}.py
│   ├── agents/{planner,writer,reviewer,memory}.py
│   ├── providers/{base,deepseek,mimo,codex}.py
│   ├── importers/{markdown,text,docx,pdf}.py
│   ├── memory/{events,states,threads,retrieval}.py
│   ├── validators/{repetition,continuity,genre}.py
│   └── schemas/{outline,change_request,chapter,event}.py
├── novels/
│   └── novel_001/
│       ├── project.yaml
│       ├── imports/original/
│       ├── bible/
│       ├── outlines/{source,proposals,approved}/
│       ├── drafts/
│       ├── chapters/
│       ├── memory/{events,states,summaries,threads}/
│       ├── approvals/
│       ├── reviews/
│       ├── exports/
│       └── .runtime/             # 任务缓存，通常 gitignored
├── data/vibenovel.sqlite3
└── tests/
    ├── fixtures/
    ├── test_import_and_outline.py
    ├── test_approval_guard.py
    ├── test_commit_and_recovery.py
    └── test_memory_and_continuity.py
```

源小说资料（Markdown/JSON）应方便手工编辑、Git 版本化；SQLite 储存查询索引、事务元数据与审批记录。变更源和索引要明确谁为权威，避免双向同步引起的分叉。推荐正式正文、锁定设定与批准大纲以版本化文件为权威，结构化数据库由带版本的提交事务管理并允许重建索引。

## 9. CLI 用户体验建议

```bash
vibenovel init my_story --genre football
vibenovel import-outline my_story ./my_outline.docx
vibenovel analyze-outline my_story
vibenovel propose-outline my_story --style ambitious
vibenovel approvals list my_story
vibenovel plan-chapter my_story 001
vibenovel draft my_story 001 --provider deepseek
vibenovel review my_story 001
vibenovel finalize my_story 001       # 需要明确人类确认
vibenovel status my_story
vibenovel export my_story --format md
```

以上为**计划设计的命令，不是 v2 或现有 v3 已实现的命令**。Codex 模式可使用自然语言发起同等任务，操作同一部作品。

## 10. 验收标准：必须通过的测试

1. **大纲保真**：导入大纲、解析和扩写后，原文 hash 不变；AI 新增设定可追溯并可识别。
2. **审批守卫**：提出“主角死亡改为存活”，未批准前不可修改已批准计划、canon、正式正文和记忆；拒绝后不改变状态。
3. **审批失效**：提案基于版本 A，而作者把故事更新到版本 B，旧批准不能直接应用。
4. **断点恢复**：第 10 章生成到第三场景失败，恢复仅补写剩余内容，不重复提交已生成段落。
5. **定稿原子性**：若人物状态写入失败，不得存在“正文已定稿但事件记忆未更新”的半提交状态；恢复后可重放。
6. **跨章节连续性**：上一章人物负伤、在古墓、武器已遗失；新章无解释恢复伤势/位置/武器时必须产生冲突提示。
7. **反重复**：同一章多次出现同一解释、连续三场比赛均以同样机制绝杀，能输出具体证据；合理伏笔呼应不能硬判为错误。
8. **多作品隔离**：小说 A 的人物、记忆、故事设定不得被小说 B 检索为事实。
9. **Codex/API 同构**：两个入口使用相同的章节编号、事件 Schema、审批状态；只更换执行适配器。
10. **多模型容错**：密钥缺失、余额不足、请求超时、无效 JSON 和不支持工具调用等错误都不会污染正式内容。
11. **导出**：至少支持按卷、按章汇总 Markdown；Word/EPUB 作为增强功能。
12. **可人工接管**：作者直接修改正文后，系统可识别文件变化并重新生成待确认记忆差异。

## 11. 实施顺序

**M0 — 在 v2 模板上建立运行基础**：项目创建、配置读取、小说隔离、命令行框架、导入 Markdown/TXT 大纲。

**M1 — 大纲工作流和审批门禁**：DOCX/PDF 导入、五级纲结构、锁定字段、L1/L2/L3 识别、提案与审批状态机、版本差异。

**M2 — 章节写作最小闭环**：上下文构建、草稿、审稿、人工定稿、事件抽取、状态快照、检查与恢复。

**M3 — 多模型/API 与 Codex 协作**：统一调用适配器、运行模式切换、成本与错误日志、模型能力识别、双端同构测试。

**M4 — 题材校验与语义检索**：四个领域约束、结构化事实校验、重复事件签名、按需 RAG。

**M5 — 使用体验**：Web UI、章节时间线、关系图、看板、EPUB/Word 导出等。

> 交付策略：每个阶段都必须能运行演示/回归测试，优先跑通一部 10 章的试验小说，而不是一开始追求百万字的全自动生成。

## 12. 推荐默认参数（均可修改）

- 创作模式：`high_autonomy`。
- L3：强制作者批准；L2：只允许当前已授权卷内部扩写，涉及人物命运或结构影响自动升级 L3。
- 长篇生成：按 **Scene → Chapter → Volume** 递进，不一次性让模型连续自由写 50 章。
- 每章目标：约 2500–3500 汉字，按题材调节；字数计算与 token 不混淆。
- 默认上下文：上一章正文 + 最近 3–5 章摘要 + 本章引用到的历史实体和事件。
- 作者定稿：逐章确认（可在系统稳定后开启批量审阅），不自动发表。
- 写作与记忆保存：Git + Markdown/JSON + SQLite；RAG 后期按需增加。
- API 模型：`deepseek-flash`、`mimo-v2.6-flash`、`mimo-v2.6-pro` 可配置，并不预设文学效果优劣。

## 13. 参考官方文档（核对日期：2026-10-18）

- DeepSeek API：https://api-docs.deepseek.com/zh-cn/
- DeepSeek V4.1 Flash：https://www.deepseek.com/en/news/deepseek-v4-1-flash/
- MiMo 模型列表：https://mimo.mi.com/docs/zh-CN/quick-start/summary/model
- MiMo Pro OpenAI 兼容示例：https://mimo.mi.com/models/en-US/mimo-v2.6-pro
- Codex AGENTS.md 工作流：https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex
- LiteLLM 跨提供方接口：https://docs.litellm.ai/docs/
