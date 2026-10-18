# M0 存储与输入契约

`schema_version: 1` 为 M0 数据格式版本，软件版本单独管理。未知版本拒绝读取；不猜测迁移。源文件是权威，完整性 hash 用于检出变化，不能证明作者批准或抵抗具备同权限的恶意修改。

## 作品

`novels/<id>/project.json` 保存 id、title、genre、创建时间和 schema_version。ID 为小写 ASCII slug，不含路径分隔符，拒绝 Windows 保留设备名。作品内相对路径统一使用 `/`，跨平台均拒绝反斜线、绝对/UNC 路径、Windows 别名及无效 Unicode。所有作品内路径先检查其组成和链接/junction，避免静态路径逃逸；这不是抵抗同 OS 权限并发换链攻击的安全沙箱。

每部作品含 `imports/original`、`bible`、`outlines/source`、`outlines/proposals`、`outlines/approved`、`drafts`、`chapters`、`memory/{events,states,summaries,threads}`、`approvals`、`reviews`、`exports`、`.runtime`。创建在同层临时目录完成后发布；不覆盖已存在目录。根目录旧小说资料不纳入 v3 查询。

正式内容指纹覆盖 `project.json` 和 `bible/`、`outlines/approved/`、`chapters/`、`memory/` 下文件的相对路径与原字节 SHA256。路径/删除/手改会改变指纹；不包含导入、候选、草稿、导出、运行缓存。它是当前内容的 fingerprint，历史回退到相同内容会得到同值。Studio 的单调 revision、历史快照与审批决定另行记录；细粒度锁定字段仍待实现。

## 原件导入

仅 MD/TXT。默认严格 UTF-8，识别 UTF-8/UTF-16 BOM；旧编码需明确指定，不猜测、不用 replacement 丢字。读取有字节上限。原件内容 hash 为导入 ID，目录 `imports/original/<sha256>/` 中三个文件共同发布：`original.<ext>`、`extracted.md`、`metadata.json`。

metadata 保存作品 ID、首次导入文件名、字节 hash、抽取 hash、编码、格式、抽取器版本、UTC 时间和行/字符来源区间；来源身份 SOURCE、信任 untrusted、canonical=false。抽取文本仅供检索/分析，不能作为项目命令执行。字符区间为解码后 Unicode 字符位置，行号从 1 起，不能直接套用于原始字节。重复内容复用首次来源记录，不覆盖；改变解码方式必须先明确处理已有导入，不静默重解释。原件默认上限 10 MiB、可配置至 100 MiB；来源最多 20,000 段、metadata 上限 8 MiB，超限时提前拒绝并提示分文件导入，不部分丢弃出处。

先在同一文件系统的临时目录准备完整 bundle，再 rename 发布。M0 的单目录发布不等于 M2 的跨文件定稿事务。校验器重算 hash、校验文件布局及来源区间；不是语义/文学审查。原件采用应用层不可改写规则和 hash 检查，不声称 Windows ACL/WORM 强制只读。暂存残留不纳入导入清单，异常残留由 verify 提示。

## 导出

只读 `chapters/chNNN.md`（至少三位、正整数），编号按数值排序；相同数字的不同补零命名报错，所选范围缺章报错。可选择起止章节。`verify` 检查从第 1 章起是否连续、正式章节是否为非空 UTF-8；未定稿导致缺号也会提示，作者可明确导出已有连续范围。目标仅是 `exports/` 内单个 Markdown 文件名，默认不覆盖；导出不创建正式章节或记忆，不包含草稿。按卷导出需未来的卷归属 Schema。

## Studio alpha 补充（3.0.0a2）

`approvals/.studio/state.json` 是版本化权威记录，保存单调 revision、历史快照、正式指纹、提案和决定（上限64 MiB）。`approvals/.studio/journal.json` 是未完成单文件事务的持久化意图；不得当缓存删除。核心获取 `.runtime/studio.lock` 的线程/进程排他锁后先恢复 journal，再读取或写入。正常短操作等待锁最多2秒，超时返回409。不能确定的漂移保留 journal 并停止写入。

单文件事务先写 journal，再发布目标文件，再发布新状态，最后删除 journal。恢复核对前后状态与目标精确哈希；批准仅消费一次。此协议不构成 chapters+多份memory 的批量原子定稿。正式 revision 与当前指纹分开：通过核心观察到的回退仍推进 revision；两次观察间完全恢复原状的外部变化无法检测。

候选/审稿直接保存绑定 expected_hash，恢复作为新版本。生成任务发布还绑定 context_sources、fingerprint、canonical_revision，在同一核心锁内检查；不将 API 输出自动升级为正式事实。已批准章计划须有 applied 提案及匹配 after_hash。

任务状态保存于 `.runtime/jobs/<id>.json`，不是正式小说事实；关闭浏览器不会删除，备份作品时也应保留未完成任务。每工作区 `.runtime/studio-jobs.lock` 阻止第二任务服务重复调用。运行请求重启变为中断，排队/暂停状态恢复；旧Codex接力任务仅保留历史且禁止继续或重试；无自动付费重试。候选返回后先持久化精确发布意图，崩溃后核对候选及来源历史，已完整保存则恢复成功；未发布则由作者显式恢复；冲突文件不覆盖。

密钥可由工作区固定的 `config/api.local.json` 或后端进程环境提供，环境变量按字段优先；配置每次能力查询与执行时读取。真实配置与备份排除 Git，只有不含密钥的 `api.example.json` 可上传。HTTP仅提供固定配置的脱敏读取和专用更新接口；已有密钥不回显，保存受同源/请求令牌和版本hash保护，任务中不保存凭据；无效配置只返回脱敏原因。任务上下文及恢复候选包含作品内容，仍应按本地作品资料管理；不应将 `.runtime/` 上传到公共仓库。UI不开放任意本机路径或运行命令。

## 创作规划补充（3.0.0a3）

规划候选是 `outlines/proposals/blueprint-<jobid>.json`，正式规划包固定为 `outlines/approved/blueprint.json`。顶层版本、作品ID、稳定记录ID、数组/文本大小、卷章范围、引用和逐行证据均受校验。运行时覆盖source_hashes；模型不能扩充未提供来源或自称AUTHOR_APPROVED。完整契约见 `blueprint_schema_v3.md`。

规划的主输入只接受导入extracted文本、来源大纲或独立候选。有限正式资料可以作为上下文核对，不把它们当作可任意重写的主输入；禁止提案目标依赖自己的旧文件版本。暂不支持将批准包自身作为生成新包的主来源，也不提供局部合并。

新提案可带 `dependencies` 与 `dependency_versions`，两者纳入提案hash；后者记录依赖的已观测历史/审批版本。创建、批准、应用及使用章计划时检查，保留旧记录hash兼容。整包批准不表示事件已发生；提取章计划另外创建单文件提案，依赖父包和来源。Studio内A→B→A不能使旧依赖批准复活；完全未观察到的外部回写仍在现有存储保证之外。

章节起草将有效父包中与本章相关的规划人物、知情边界、文风、伏笔及时间线作为带范围说明的上下文，绑定父包hash。不能以父包规划替代正式章节事实。规划生成的无效JSON可保留在任务repair_candidate供人工修正，不发布到候选文件；修复也必须通过相同来源与结构检查，不自动发起新付费请求。

未应用的已批准提案允许作者撤销批准，保留旧决定及审计事件；应用后的正式变更不能通过撤销按钮回滚，须重新创建提案。

## 写作检查与设置补充（3.0.0a4）

写前包不另存小说事实；分项来源、缺失与字符预算返回页面，任务保留来源hash与样章配置hash。报告保存于版本化 `reviews/analysis-<id>.json`，逐项状态变更通过同一锁和journal；正式内容、样章或被引用正文变化使旧报告失效，失效报告不可标已处理。固定样章存于 `.runtime/style-baseline.json`，包含作者明确选定的版本快照，不因源文变化自动换样本。

生成与分析发布支持固定运行依赖 `.runtime/style-baseline.json` 的hash（未配置为null），在文档写入同一锁内核对，不开放任意运行文件读取。历史检索只读本作品正式章节，返回原句/行号/hash及未扫描范围；字词分数不证明事实或情节相同。

`config/api.local.json` 的专用更新使用工作区锁、expected_hash和原子替换。密钥空输入保留、显式清除才删除；更换地址需新密钥或清除。读取只返回地址/模型/has_api_key及环境覆盖字段名，响应不含已保存密钥或片段。旧配置中的codex默认值兼容读取为api，不恢复旧执行器。


## DSH能力完善补充（3.0.0a5）

`.runtime/writing-toolkit.json` 保存作者明确登记的词表、别名、禁用/必用提示、人物检索名、伏笔关键词范围和文风要求；这些是检查偏好，不是正式设定或实际发生记忆。乐观保存绑定expected_hash，最大512KiB、每类200项。锚段最多5处，限本作品chapters/drafts，来源hash与行号、原句严格核对；过期不自动换样，也不作为角色知情或历史事件依据。全局词库不自动合并，可由作者显式复制JSON。

新分析报告schema_version=2，兼容读取旧schema1而不静默迁移。新增逐规则found/returned/truncated、实际文风比较样本子集和runtime_dependencies。排除目标本身后以余下固定快照比较；人工resolved只表示作者标记。verify保存当前新报告，逐项返回remaining/not_found/unchecked，旧报告与作者标记保持；检查域、依赖或截断不确定时不假报解决。

运行依赖仅允许 `.runtime/style-baseline.json` 与 `.runtime/writing-toolkit.json` 的非空子集，未配置hash为null，任务/报告发布在同一文档锁内核对。锚段另绑定实际来源hash。写前包统一所选来源及区间manifest；compact/full均不额外补回已附的全文。任务记录最终提示字符数与220000总预算，预览仅对应材料包，作者任务和通用规则的额外字符在任务预算中明确列出。

记忆过去时点只接受严格chNNN（可带版本/日期后缀），或JSON顶层chapter、Markdown frontmatter chapter整数。章号冲突、无时点、未来章记录列为遗漏；记录内部误写未来事实仍需作者复核。结构看板只统计正式章字面提及和章号差，不自动写入实际人物状态或已回收伏笔。

本地语义固定使用Xenova/bge-small-zh-v1.5转换版本 `75c43b069aac4d136ba6bc1122f995fedcfd2781`（原模型BAAI/bge-small-zh-v1.5，MIT）。安装显式执行，模型与分词器均校验固定SHA256；正常查询没有下载或模型API请求。可选ONNX推理在限时隔离子进程执行，只接收有界文本并返回512维归一化向量。模型目录 `.runtime-build/models/bge-small-zh-v1.5/` 及每作品 `.runtime/semantic-index.json` 都是可重建派生数据，不能代替Markdown/JSON权威。

语义缓存绑定模型版本和片段hash，只保存向量，不从缓存返回原句/路径；每次返回当前正式正文的精确hash/行号/列号/片段。缓存损坏则重建，推理缺项/失败明确报错，不暗中退化为字面模式。检索前后正式revision/fingerprint变化则拒绝结果；界面刷新比对来源版本后禁用旧行号跳转，打开文稿再核对expectedHash。仍不宣称检测两个观测之间完全恢复原状的外部修改。

诊断接口只输出受限枚举和固定计数、版本、相对章节路径；不序列化任务error/prompt、小说正文/书名/配置正文或任意自由文本。此报告可用于反馈；正文检索与检查报告仍属于私人作品资料。
