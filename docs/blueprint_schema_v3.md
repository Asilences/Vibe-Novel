# 创作规划包：本轮接口契约

这是软件数据契约，不批准小说情节。规划包只代表未来策划；人物关系图、伏笔与时间线均标明规划性质，不能当作已发生事件或角色已经知道的事实。

## JSON

顶层严格字段：`schema_version: 1`、`novel_id`、`source_hashes`（作品内路径→SHA256，由任务运行时覆盖）、`book`、`volumes`、`chapters`、`characters`、`relationships`、`threads`、`timeline`、`settings`、`style`、`unknowns`（字符串数组）。

通用记录字段：`id`（ASCII字母开头的稳定slug）、`title`、`summary`、`basis`（`source_asserted` / `inferred` / `suggested`）、`evidence`。每条 evidence 严格为 `{path, start_line, end_line, quote}`；行号从1开始，quote须非空并与绑定版本的该行区间精确匹配。`source_asserted` 必须有证据；另外两类允许空证据。所有记录均待作者审阅，不接受模型自称 AUTHOR_APPROVED。

各字段补充：

| 字段 | 形态与额外字段 |
| --- | --- |
| book | 单条通用记录 |
| volumes | 通用记录数组，另有 `number`、`chapter_start`（正整数）、`chapter_end`（正整数或null，表示开放末卷）；编号/区间不重叠 |
| chapters | 通用记录数组，另有 `number`、`volume_id`、`goal`、`conflict`、`turn`、`outcome`、`beats`（字符串数组）、`character_ids`、`depends_on`（章ID数组）、`thread_ids`；引用必须存在，不允许依赖自己或后续章；章号须在所属卷范围内 |
| characters | 通用记录数组，另有 `role`、`knowledge`（规划的知情边界，不代表已知事实） |
| relationships | 通用记录数组，另有 `from_id`、`to_id`（人物ID），不能指向不存在人物 |
| threads | 通用记录数组，另有 `setup_chapter`、`payoff_chapter`（正整数或null），回收不能早于铺设 |
| timeline | 通用记录数组，另有 `time`（原文时间标签，不强行猜公历）、`chapter`（正整数或null）、`character_ids`、`status`（只能为 `planned`） |
| settings | 通用记录数组，另有 `category`（`world` / `location` / `organization` / `item` / `rule` / `domain`） |
| style | 单条通用记录，另有 `rules`、`avoid`（字符串数组）；只描述可操作写法，不复制特定作品独特表达 |

允许未规划的数组为空，unknowns明确待确认项。记录ID全包唯一。体积不超过文稿上限，数组、字符串、引用数量须有界。任何缺字段、未知字段、重复JSON键、无效引用或伪造出处均拒绝，不部分保存。

## 核心接口与HTTP

`vibenovel.core.blueprints` 提供：

- `prepare_blueprint(project, text, source_hashes) -> str`：解析模型JSON（可兼容单个json围栏）、补全novel_id（若模型给出其他作品则拒绝）并覆盖source_hashes，严格校验后返回规范JSON。只处理候选，不写文件。
- `PlanningStore(project).list() -> list`：列出规划候选与当前批准包的轻量元数据（可含失效/格式错误状态），不隐藏坏文件，不批量返回正文。
- `PlanningStore(project).read(path) -> dict`：返回 `{path, hash, content, data, valid, issues, approved, stale}`。批准仅指匹配当前已应用提案；非法/失效仍可查看，不当作有效规划。
- `PlanningStore(project).propose(path, expected_hash, chapter_id=None) -> proposal`：整包目标固定为 `outlines/approved/blueprint.json`；仅从有效且已应用整包抽取章计划，目标固定 `outlines/approved/chNNN.md`。创建提案，不批准、不应用。
- 提案新增可选 `dependencies: {path: sha256}` 与核心记录的 `dependency_versions`，绑定hash与已观测版本并在创建、批准、应用、`approved_outline`时验证，兼容旧审批记录。拒绝目标自依赖。包绑定来源；提取章计划另绑定父规划包，父包变更后阻止起草。

HTTP：

- `GET /api/projects/{id}/planning` → `{packages: PlanningStore.list()}`。
- `GET /api/projects/{id}/planning/package?path=…` → `PlanningStore.read()`。
- `POST /api/projects/{id}/planning/proposal`，JSON `{path, expected_hash, chapter_id?: str}` →201提案。
- 新增 `build_blueprint` 任务；沿用 jobs 请求，另有可选 `planning_start`（默认1）、`planning_count`（默认3，上限10）。主输入限导入extracted、outlines/source与outlines/proposals；正式资料仅作有限上下文校验，避免批准包自依赖。生成全书/卷级概要与这段章计划，以及有依据的规划资料，不要求一次写完全书。输出路径由后端固定为 `outlines/proposals/blueprint-<jobid>.json`；解析失败不发布。API、Codex接力、候选崩溃恢复共用规范化/校验。

前端提供创作规划工作区：选来源、起始章/本批章数、执行方式、任务状态；规划候选选择、卷章树、人物表、关系图、伏笔、计划时间线、世界设定、文风、待确认点和出处。创建提案后转审批中心；逐章提取提案，禁止提前批量创建多份提案导致revision立即过期。正式memory仍单独浏览，不自动写入。
