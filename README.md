# VibeNovel v3 — 本地 AI 小说工作台

当前版本 **3.0.0a7 / Studio alpha**：独立的本地 Web 小说工作台。作品导航、文稿编辑器与创作助手三栏布局，支持作品管理、大纲导入、结构化创作规划、写前上下文包、本地审稿、固定样章、历史检索、已有小说修订、多模型候选协作、作者盲评、文稿版本比较恢复、改编审批和持久任务。CLI 核心仍支持 Python **3.9+**、标准库离线运行。

生成统一使用 **API**，通过「多模型协作」管理模型、密钥、角色和预算；未迁移用户也可使用旧「模型与 API 设置」。Codex交互接力已移除；旧任务只保留历史。创作规划提供卷章结构、人物表与规划关系图、伏笔和计划时间线；可选本地BGE语义检索已提供。实际定稿事件图谱、Tiptap富文本/行内批注、多文件原子定稿、DOCX/PDF和桌面封装尚未实现。

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

Studio API 的socket等待超时为180秒，并非严格总耗时上限。取消立即阻止结果发布；已发出的网络请求可能继续占用工作槽直到返回或超时，不自动重复付费调用。

## 从大纲生成卷纲、章纲与设定

进入「创作规划」，选择该作品已导入的大纲或独立候选大纲，指定本批起始章和章数（1–10，默认3章）后生成。规划包同时整理全书/卷概要、这段章计划、人物、关系、伏笔、未来事件、世界/题材设定、文风与待确认项。优先提取原稿已有内容，区分「原稿陈述」「推断」「新增建议」；原稿陈述须绑定来源版本、原文行与摘录。不要求一次生成长篇所有细纲。

查看并修改候选后，提交整包提案到审批中心，批准并应用。随后在有效的已批准包中选择一章，提取独立章计划提案，再批准应用，才能据此起草。父规划或依赖版本改变后，需要重新确认相关章计划；当前是逐文件审批，不是批量原子定稿。结构无效或出处不匹配的模型JSON不发布，可修正回填，不自动重调付费接口。

关系图与时间线展示的是规划，不能把将来发生的事件、秘密或伏笔当作正式记忆。实际记忆仍依据作者定稿。查看[规划工作台审计](docs/planning_workbench_v3.md)与[数据契约](docs/blueprint_schema_v3.md)了解范围与后续工作。

## 配置 API

以下为尚未迁移的单API方式。启用多模型注册表后，任务改用「多模型协作」内的独立配置与角色绑定，旧单API文件及其环境变量不再覆盖新表。首次使用多模型时的迁移步骤见下方“多模型配置与职责绑定”。

点击 Studio 的「模型与 API 设置」，选择 DeepSeek、MiMo、GLM、豆包或自定义兼容接口，再选择或填写模型 ID 和 API key。密钥使用密码输入框，保存后清空输入，已保存密钥只显示配置状态。留空保留原密钥；勾选「清除密钥」删除本机配置中的密钥。更换接口地址时须提供新密钥或明确清除旧密钥。预设模型是填写建议，实际可用性以服务商控制台为准。

设置保存到工作区 **`config/api.local.json`**，不需重启，也不自动发起模型请求。多个页面或手工修改发生冲突时，须重新读取配置后再保存。仍可将 `config/api.example.json` 复制到该路径，手工填写 UTF-8 JSON：

```json
{
  "default_provider": "api",
  "api": {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key": ""
  }
}
```

换服务商时填写对应的 OpenAI 兼容 Base URL、实际模型 ID 和 API key。JSON 不支持注释，请保留双引号。密钥文件及其备份已加入 `.gitignore`，不要把真实密钥填入会上传的 `api.example.json`。未配置时提示填写 API，不回退到其他执行方式。

以下为 2026-10-18 核实的普通按量 API 填写示例，实际权限和模型 ID 以各自控制台为准：

| 厂商 | `base_url` | `model` 示例或填写方式 |
| --- | --- | --- |
| [DeepSeek](https://api-docs.deepseek.com/zh-cn/) | `https://api.deepseek.com` | `deepseek-flash`；也可按已开通模型改为 `deepseek-v4-pro` |
| [小米 MiMo](https://mimo.mi.com/static/docs/quick-start/summary/first-api-call.md) | `https://api.xiaomimimo.com/v1` | `mimo-v2.6-pro` 或 `mimo-v2.6-flash` |
| [智谱 GLM](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) | `https://open.bigmodel.cn/api/paas/v4` | 例如 `glm-5.3`，按实际开通模型填写 |
| [豆包 / 火山方舟](https://github.com/volcengine/volcengine-python-sdk/blob/master/volcenginesdkexamples/volcenginesdkarkruntime/completions.py) | `https://ark.cn-beijing.volces.com/api/v3` | 复制控制台调用示例中的 `model`，通常为 `ep-…` 推理接入点 ID |

应用自动追加 `/chat/completions`。这几家共用同一适配器，已测试兼容协议；本次未调用其真实付费服务。上表使用普通按量地址；MiMo Token Plan、GLM Coding Plan 等套餐可能有专用地址、密钥和使用范围，请以对应套餐官方说明为准，不混用套餐与按量密钥。

未迁移的单API方式也兼容启动后端前设置环境变量，**环境变量按字段优先于旧 JSON**；若旧文件修改未生效，先检查是否仍设置了对应环境变量。启用多模型注册表后不再使用这些环境覆盖：

```powershell
$env:VIBENOVEL_API_BASE = 'https://你的兼容接口/v1'
$env:VIBENOVEL_API_MODEL = '服务商提供的模型ID'
# 用隐藏输入读取 key，不把实际 key 写进命令历史或项目文件
$studioApiKey = Read-Host 'API key' -AsSecureString
$env:VIBENOVEL_API_KEY = [System.Net.NetworkCredential]::new('', $studioApiKey).Password
.venv/Scripts/python.exe tools/studio.py
Remove-Item Env:VIBENOVEL_API_KEY
```

支持 OpenAI 兼容 `chat/completions`，不保证每个服务商所有扩展参数兼容。仅点击开始任务才调用；返回文本只保存到候选或审稿文件，不提供模型文件/Shell 工具。外部接口要求 HTTPS；本地兼容服务须在设置页启用本机回环地址，或设置 `VIBENOVEL_ALLOW_LOOPBACK_API=1`。usage 仅展示服务商实际返回值，无报价时成本未知。输入密钥仅用于本机设置保存，不放入浏览器持久存储、URL、任务或日志；后端不会把已保存密钥返回页面。

## 写作检查

「写作检查」提供四个工作区：

- **写前包**：选择有效的已批准章计划，查看前章承接、近期正文和摘要、实际状态/事件/伏笔、已确认设定与父规划。精简/完整模式均有预算，缺失与未纳入资料明确列出。章节起草使用同一装配逻辑，任务中可选择模式；生成时重新核对批准和来源版本。
- **本地审稿**：检查已保存草稿或正文的套话、长句段、字面重合及固定样章句长差异。报告保留行号、原句、相关来源及建议，可标记待处理/已处理/忽略；正文或依赖变化后报告失效，需重新分析。
- **固定样章**：作者显式选定 1–10 份认可的样章。保存版本快照；原文变化后提示失效，不自动替换基线或改用全书。
- **历史检索**：仅搜索本作品正式章节，返回原句、行号和版本。当前采用中文二元字组/词项匹配，最多扫描 80 份文件/8 MiB；遗漏明确列出。这不是语义检索或剧情重复判定。

这些检查无需 API，产物只进入报告与本地分析配置。统计与命中都是编辑线索，不代表文学质量或一致性结论。设计吸收了 [dsh-novel-writer](https://github.com/siweina/dsh-novel-writer) 的有用思路，本轮以 VibeNovel 原生核心实现，未引入其 MCP 写工具、Node 运行依赖或模型资产。详见[集成设计与验收](docs/writing_analysis_v3.md)。

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

安装构建需 setuptools；运行核心仅用标准库。`--workspace` 放在子命令之前，默认当前目录，目录必须已经存在。CLI 配置读取该工作目录的 `config/app.json`，不存在则采用相同默认值；`execution.mode` 为 `api`，读取旧版 `codex_interactive` 元数据时归一为 API，不启用旧接力功能。其他未知版本、字段或模式均报错。Studio 的 API 任务使用前述本地 JSON 或环境变量配置。

```bash
python tools/vibenovel.py config
```

成功结果为 JSON（`--help`/`--version` 除外）；退出码 `0` 为成功、`1` 为完整性检查发现问题、`2` 为输入/配置/操作错误。`status` 的 `fingerprint` 是正式内容的当前指纹，不是批准记录或完整历史版本。`verify` 是磁盘完整性检查，`quality_review: not_run` 表示没有运行文学/连续性审稿。

## DSH 写作能力完善（3.0.0a5）

在「写作检查」中使用六个页面：写前包、本地审稿、固定样章、历史检索、用词与档案、跨章线索。词表与作者文风规则以本作品JSON配置；固定锚段须明确选择来源版本和行号。复查生成新报告，区分“仍命中／未再命中／未验证”，保留原报告的作者标记。跨章看板提供人物提及空档、伏笔关键词跨度和章计划覆盖，并可生成不含正文的诊断JSON。

历史检索默认字面词项，可切换真正的**本地语义检索**。首次安装需下载固定版本约24MB量化模型（加约0.4MB分词器）：

```powershell
python -m pip install -e ".[studio,test,semantic]"
python tools/setup_semantic.py --proxy http://127.0.0.1:7890
```

不使用代理时省略 `--proxy`。本机已完成安装和真实推理验证；新电脑需执行上述命令。安装脚本仅下载公共模型，不读取小说或API密钥；正常查询不联网、不调用付费API。可选推理依赖采用已验证的ONNX Runtime 1.20.1，并在独立进程中运行，失败不会终止Studio。

模型保存在工作区 `.runtime-build/models/bge-small-zh-v1.5/`，章节向量缓存位于作品 `.runtime/semantic-index.json`；两者均不上传Git。删除缓存后可从正式正文重建。单次扫描80份/8MiB正式章节、最多4000块（每块200字符），遗漏和截断均会说明；相似度提供相关候选，不证明剧情相同、矛盾或伏笔回收。

完整20项DSH能力对照、有意替代及未实现部分见 [DSH吸收说明](docs/dsh_absorption_v3.md)。DSH阶段未启用多模型编排；后续显式多模型功能见下文，多文件事务定稿仍待实现。

## 已有小说分析与修订（3.0.0a6）

在侧栏打开「已有小说修订」，导入已经写好的MD/TXT整稿，或选择已保存的Markdown来源。先核对自动分章预览；前言会单独保留，特殊章标题可用原稿起始行手工校正。点击创建后生成独立修订副本，原件保留。每个会话最多80段/8MiB，每段最多20万字符；长稿可按卷导入。

选择章节，可运行本地检查、勾选多章或明确逐批检查全稿。每批最多20个目标/50万目标字符，对照当前修订会话的章节，显示实际覆盖和遗漏；本地检查提供套话、长句、作者词表、固定样章差异和跨章字面重合证据，不能认定整部小说无矛盾或可靠识别AI来源。

配置API后，可先做证据审稿，再选择「减少套话、减少重复表达、增加动作与感知、自然对话、保留原有文风」及保守/标准/深入强度，生成修改候选。拖选文本或输入Unicode字符区间可仅改选区；后端保留区间外的原文。审阅完整章节差异后，点击「接受到本章修订副本」，复查并独立导出。来源、候选、配置或参照版本变化会阻止接受旧候选。

这里的“降AI味”是作者选择的表达编辑目标，不提供AI概率、检测分数或通过检测保证。API审稿/改写会发送界面列明的正文范围与表达参照给已配置服务商；本地检查不调用生成API。任何候选仍须作者核对，正式正文与故事记忆继续通过原审批流程更新。详见[已有小说修订设计与验收](docs/manuscript_revision_v3.md)。

## 多模型配置与职责绑定（3.0.0a7）

在侧栏打开「多模型协作」。已有单API用户先点击「复制原 API 配置」，将当前配置显式复制成 `default_api`；原来的 `config/api.local.json` 保留。然后添加所需模型、填写对应API key，并在本作品内分别绑定规划、正文、审稿、候选记忆角色。可以让一个模型负责所有角色，也可以选择不同服务商；每个生成节点由单一实际模型返回，后续修订可按允许主备选择并保留来源链。保存配置不会发出模型请求，界面不会回显已保存密钥。

真实多模型配置在工作区 `config/models.local.json`，已排除Git。希望直接填JSON时，可将 [models.example.json](config/models.example.json) 复制为该文件；仅在目标文件不存在时复制，已有配置通过页面修改。示例包含DeepSeek、MiMo、GLM、豆包四个空配置。分别填写实际model ID与各自的 `api_key`，并按服务商文档核实 `context_tokens`、`max_output_tokens`；示例数值不代表已验证容量。报价 `price_input_usd`/`price_output_usd` 按每百万Token填写，不清楚时保留null；豆包可填写自己的 `ep-…` 接入点。最后在页面保存角色绑定，默认 `default_api` 不会自动改绑到示例中的任意模型。

作品策略同时设置共享的最多调用数、Token预算、可选美元预算及允许备用顺序。默认只允许一次调用；编辑→审稿→候选记忆至少需要3次，技术备用和重试另占用调用次数，须先保存足够预算。金额未知会显示“未知”，设置美元上限时须提供对应模型报价。路由预览显示首节点实际主备模型、输入估计与上限，任务记录保存冻结快照；排队期间改配置会使旧任务停止，运行中的模型不会被设置页悄悄替换。普通任务明确重试沿用原预算组，不重置余额；工作流节点禁止从通用任务页重试，须核对原流程和不确定费用后明确新建。

技术备用只在作者列明的目的地、次数和剩余预算内处理限流或部分服务器错误；认证失败、超时和来源冲突会停止。取消、超时或重启不确定是否已经计费时保留预留，不自动再次调用。所有结果仍是候选，API审稿与候选记忆不代替作者批准，也不自动写入正式正文/记忆。完整持久化和预算边界见[存储契约](docs/storage_contract_v3.md)。

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
- [DSH吸收审计](docs/dsh_integration_audit_v3.md)：原始问题与后续修复证据。
- [Studio 设计审计](docs/studio_design_v3.md)：前端、API、任务、审批与阶段边界。
- [创作规划工作台](docs/planning_workbench_v3.md)：卷章、设定、关系、伏笔、文风和来源审批。
- [实施计划](docs/implementation_plan_v3.md)：M0–M5 范围、状态及需求追踪。
- [存储契约](docs/storage_contract_v3.md)：来源、指纹、路径与发布边界。
- [原始 v3 设计](docs/VibeNovel_v3_original.md)：原稿留档，其中命令不代表已经实现。
- [v2 使用指南](docs/v2_guide.md)：原模板工作流；三个旧工具继续只处理根目录 v2 作品。

正式小说仍遵循 `AGENTS.md` 和 `prompt.md`：计划批准后写草稿，作者定稿后才更新正式正文与记忆。不要把未经确认的草稿或导入推断当成已发生的事实。
