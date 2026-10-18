# VibeNovel v3 — 本地 AI 小说工作台

当前版本 **3.0.0a2 / Studio alpha**：独立的本地 Web 小说工作台。作品导航、文稿编辑器与创作助手三栏布局，支持作品管理、大纲导入/候选扩写、文稿版本比较恢复、改编审批、故事资料和持久任务。CLI 核心仍支持 Python **3.9+**、标准库离线运行。

API 模式可自动生成候选；Codex 模式为**交互接力**：导出上下文，在 Codex 执行后回填结果。App Server 自动会话、Tiptap 富文本/行内批注、多文件原子定稿、人物关系图/伏笔时间线、语义检索、DOCX/PDF 和桌面封装尚未实现，不显示虚构进度或成本。

## 启动 Studio

首次在项目根目录执行（前端构建需要 Node.js 22.12+，推荐 24）：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[studio,test]"
cd studio
npm ci
npm run build
cd ..
.venv/Scripts/python.exe tools/studio.py
```

打开 [本地 Studio](http://127.0.0.1:8000)。后续只需最后一条启动命令；已安装入口也可使用 `vibenovel-studio --workspace <作品工作目录> --port 8000`。默认读取当前目录下 `novels/`，不自动创建示例作品、不迁移 v2。源码版默认读取 `studio/dist`；在其他位置部署时用 `--frontend <dist绝对路径>`。发行 wheel 不内置前端资源。

使用顺序：创建作品并选择题材 → 导入或编写大纲 → 提交大纲提案 → 作者批准并应用 → 选定已批准章计划起草候选 → 独立审稿/修改 → 提交章节定稿提案 → 作者批准并应用 → 导出正式章节。故事记忆可逐文件提案更新；**章节与多份记忆暂不构成一个原子定稿事务**。草稿和导入材料不会自动成为正式事实。

编辑器保存 Markdown 原文，阅读视图清洗 HTML；保存、恢复和生成都绑定已读版本，旧写入返回冲突并保留当前输入。正式正文和记忆只读，修改或恢复须创建提案。正式内容变化后，旧提案需重新基于当前文件提交。版本历史暂限每作品 64 MiB；编辑文件 2 MiB，网页原件默认 10 MiB，超大材料请按章拆分。

已保存状态和任务都在本地。关闭网页不会停止后端任务；停止后端会中断正在请求的任务，重启后需要明确重试，不自动重复付费调用。排队任务可以暂停/继续，运行任务暂停后转为中断。已返回但尚未发布的候选提供“恢复已生成候选”，不会再次调用模型。

## 配置 API（可选）

在启动后端的 PowerShell 设置环境变量，然后运行 Studio；不设置时可用离线编辑和 Codex 接力：

```powershell
$env:VIBENOVEL_API_BASE = 'https://你的兼容接口/v1'
$env:VIBENOVEL_API_MODEL = '服务商提供的模型ID'
# 用隐藏输入读取 key，不把实际 key 写进命令历史或项目文件
$studioApiKey = Read-Host 'API key' -AsSecureString
$env:VIBENOVEL_API_KEY = [System.Net.NetworkCredential]::new('', $studioApiKey).Password
.venv/Scripts/python.exe tools/studio.py
Remove-Item Env:VIBENOVEL_API_KEY
```

支持 OpenAI 兼容 `chat/completions`，不保证每个服务商所有扩展参数兼容。仅点击开始任务才调用；返回文本只保存到候选或审稿文件，不提供模型文件/Shell 工具。外部接口要求 HTTPS；本地兼容服务须显式设置 `VIBENOVEL_ALLOW_LOOPBACK_API=1`。usage 仅展示服务商实际返回值，无报价时成本未知。API key 不发往浏览器或写入任务记录。

服务只监听本机，限制 Host/Origin、变更请求令牌和文档路径；这是单用户本地工作流，不是对同权限本机进程的人类身份认证或安全沙箱。不要将服务反向代理到公网。开发时 `npm run dev` 代理 `/api` 到 8000，使用同一台电脑访问；正式使用推荐前后端同源构建模式。

## 快速使用

在项目根目录执行（ID 使用小写 ASCII，中文书名用 `--title`）：

```bash
python tools/vibenovel.py --help
python tools/vibenovel.py init my_story --genre adventure --title "我的小说"
python tools/vibenovel.py list
python tools/vibenovel.py import-outline my_story ./my_outline.md
python tools/vibenovel.py status my_story
python tools/vibenovel.py verify my_story
```

题材可选 `football`、`adventure`、`campus`、`alternate_history`。新作品位于 `novels/my_story/`；不会迁移或覆盖根目录的 v2 资料。首次规划仍需作者确认，导入大纲也不会自动成为批准大纲。

默认严格 UTF-8，自动识别 UTF-8/UTF-16 BOM。旧编码需要明确指定：

```bash
python tools/vibenovel.py import-outline my_story ./old_outline.txt --encoding gb18030
```

原件按 SHA256 保留原始字节，与抽取文本、来源区间和 metadata 一起发布。重复内容复用首次来源记录；导入文本标为 `SOURCE / untrusted / canonical=false`。不执行原稿里的指令。应用不会覆盖原件；`verify` 能检出磁盘内容变化，但不提供操作系统强制只读保护。

## 正式章节导出

仅在作者已确认、正式章节存在于该作品 `chapters/ch001.md` 等文件后：

```bash
python tools/vibenovel.py export my_story --format md
python tools/vibenovel.py export my_story --start 2 --end 3 --output selected.md
```

输出限定在作品 `exports/` 中，默认不覆盖；显式 `--force` 可替换同名导出文件。章节按数字排序，缺章和重复编号报错；不导出草稿。M0 不提供自动定稿命令，也不代替作者判断目录中文件是否已经获批。按卷导出待卷归属结构实现后开放。

## 安装和配置

免安装脚本已可使用，也可以安装命令入口：

```bash
python -m pip install -e .
vibenovel --version
vibenovel --workspace /path/to/workspace list
python -m vibenovel --help
```

安装构建需 setuptools；运行核心仅用标准库。`--workspace` 放在子命令之前，默认当前目录，目录必须已经存在。配置读取该工作目录的 `config/app.json`，不存在则采用相同默认值。未知版本、未知字段或当前不支持的 API 模式均报错；不会静默忽略。

```bash
python tools/vibenovel.py config
```

成功结果为 JSON（`--help`/`--version` 除外）；退出码 `0` 为成功、`1` 为完整性检查发现问题、`2` 为输入/配置/操作错误。`status` 的 `fingerprint` 是正式内容的当前指纹，不是批准记录或完整历史版本。`verify` 是磁盘完整性检查，`quality_review: not_run` 表示没有运行文学/连续性审稿。

## 开发与验收

```bash
python tools/run_tests.py
python tools/check_project.py
# 包含 HTTP 边界测试（需安装可选依赖）
.venv/Scripts/python.exe tools/run_tests.py
cd studio
npm run build
```

测试全部使用临时目录，不修改真实作品。[GitHub Actions CI](.github/workflows/tests.yml)覆盖 Windows/Linux 和 Python 3.9/3.13，在 PR 更新及 `main` 推送时运行；同一分支的新提交会取消过时运行。[云端结果](https://github.com/Asilences/Vibe-Novel/actions)与本地验收记录分别查看。部分 Windows 环境无符号链接权限时会显示跳过，并另测 junction 边界。

- [设计审计](docs/design_audit_v3.md)：发现、证据与修正。
- [Studio 设计审计](docs/studio_design_v3.md)：前端、API、任务、审批与阶段边界。
- [实施计划](docs/implementation_plan_v3.md)：M0–M5 范围、状态及需求追踪。
- [存储契约](docs/storage_contract_v3.md)：来源、指纹、路径与发布边界。
- [原始 v3 设计](docs/VibeNovel_v3_original.md)：原稿留档，其中命令不代表已经实现。
- [v2 使用指南](docs/v2_guide.md)：原模板工作流；三个旧工具继续只处理根目录 v2 作品。

正式小说仍遵循 `AGENTS.md` 和 `prompt.md`：计划批准后写草稿，作者定稿后才更新正式正文与记忆。不要把未经确认的草稿或导入推断当成已发生的事实。
