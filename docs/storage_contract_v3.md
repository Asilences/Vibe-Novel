# M0 存储与输入契约

`schema_version: 1` 为 M0 数据格式版本，软件版本单独管理。未知版本拒绝读取；不猜测迁移。源文件是权威，完整性 hash 用于检出变化，不能证明作者批准或抵抗具备同权限的恶意修改。

## 作品

`novels/<id>/project.json` 保存 id、title、genre、创建时间和 schema_version。ID 为小写 ASCII slug，不含路径分隔符，拒绝 Windows 保留设备名。作品内相对路径统一使用 `/`，跨平台均拒绝反斜线、绝对/UNC 路径、Windows 别名及无效 Unicode。所有作品内路径先检查其组成和链接/junction，避免静态路径逃逸；这不是抵抗同 OS 权限并发换链攻击的安全沙箱。

每部作品含 `imports/original`、`bible`、`outlines/source`、`outlines/proposals`、`outlines/approved`、`drafts`、`chapters`、`memory/{events,states,summaries,threads}`、`approvals`、`reviews`、`exports`、`.runtime`。创建在同层临时目录完成后发布；不覆盖已存在目录。根目录旧小说资料不纳入 v3 查询。

正式内容指纹覆盖 `project.json` 和 `bible/`、`outlines/approved/`、`chapters/`、`memory/` 下文件的相对路径与原字节 SHA256。路径/删除/手改会改变指纹；不包含导入、候选、草稿、导出、运行缓存。它是当前内容的 fingerprint，历史回退到相同内容会得到同值。M1 的 revision 另含提交链、审批配置、锁定约束及决定。

## 原件导入

仅 MD/TXT。默认严格 UTF-8，识别 UTF-8/UTF-16 BOM；旧编码需明确指定，不猜测、不用 replacement 丢字。读取有字节上限。原件内容 hash 为导入 ID，目录 `imports/original/<sha256>/` 中三个文件共同发布：`original.<ext>`、`extracted.md`、`metadata.json`。

metadata 保存作品 ID、首次导入文件名、字节 hash、抽取 hash、编码、格式、抽取器版本、UTC 时间和行/字符来源区间；来源身份 SOURCE、信任 untrusted、canonical=false。抽取文本仅供检索/分析，不能作为项目命令执行。字符区间为解码后 Unicode 字符位置，行号从 1 起，不能直接套用于原始字节。重复内容复用首次来源记录，不覆盖；改变解码方式必须先明确处理已有导入，不静默重解释。原件默认上限 10 MiB、可配置至 100 MiB；来源最多 20,000 段、metadata 上限 8 MiB，超限时提前拒绝并提示分文件导入，不部分丢弃出处。

先在同一文件系统的临时目录准备完整 bundle，再 rename 发布。M0 的单目录发布不等于 M2 的跨文件定稿事务。校验器重算 hash、校验文件布局及来源区间；不是语义/文学审查。原件采用应用层不可改写规则和 hash 检查，不声称 Windows ACL/WORM 强制只读。暂存残留不纳入导入清单，异常残留由 verify 提示。

## 导出

只读 `chapters/chNNN.md`（至少三位、正整数），编号按数值排序；相同数字的不同补零命名报错，所选范围缺章报错。可选择起止章节。`verify` 检查从第 1 章起是否连续、正式章节是否为非空 UTF-8；未定稿导致缺号也会提示，作者可明确导出已有连续范围。目标仅是 `exports/` 内单个 Markdown 文件名，默认不覆盖；导出不创建正式章节或记忆，不包含草稿。按卷导出需未来的卷归属 Schema。

## Studio alpha 补充（3.0.0a2）

`approvals/.studio/state.json` 是版本化权威记录，保存单调 revision、历史快照、正式指纹、提案和决定（上限64 MiB）。`approvals/.studio/journal.json` 是未完成单文件事务的持久化意图；不得当缓存删除。核心获取 `.runtime/studio.lock` 的线程/进程排他锁后先恢复 journal，再读取或写入。正常短操作等待锁最多2秒，超时返回409。不能确定的漂移保留 journal 并停止写入。

单文件事务先写 journal，再发布目标文件，再发布新状态，最后删除 journal。恢复核对前后状态与目标精确哈希；批准仅消费一次。此协议不构成 chapters+多份memory 的批量原子定稿。正式 revision 与当前指纹分开：通过核心观察到的回退仍推进 revision；两次观察间完全恢复原状的外部变化无法检测。

候选/审稿直接保存绑定 expected_hash，恢复作为新版本。生成任务发布还绑定 context_sources、fingerprint、canonical_revision，在同一核心锁内检查；不将 API/Codex 输出自动升级为正式事实。已批准章计划须有 applied 提案及匹配 after_hash。

任务状态保存于 `.runtime/jobs/<id>.json`，不是正式小说事实；关闭浏览器不会删除，备份作品时也应保留未完成任务。每工作区 `.runtime/studio-jobs.lock` 阻止第二任务服务重复调用。运行请求重启变为中断，排队/暂停/接力状态恢复；无自动付费重试。候选返回后先持久化精确发布意图，崩溃后核对候选及来源历史，已完整保存则恢复成功；未发布则由作者显式恢复；冲突文件不覆盖。

密钥仅在后端进程环境，任务中不保存环境凭据。任务上下文及恢复候选包含作品内容，仍应按本地作品资料管理；不应将 `.runtime/` 上传到公共仓库。UI不开放任意本机路径或运行命令。
