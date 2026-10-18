# VibeNovel v3 — 本地 AI 小说工作台

当前版本 **3.0.0a1 / M0**：已从 v2 模板升级出可运行的离线 CLI 基础。支持多作品隔离、MD/TXT 大纲原件导入、来源追溯、完整性检查和正式章节 Markdown 导出。Python **3.9+**，无第三方运行依赖，不需要 API key。

自动大纲分析、审批引擎、模型续写、记忆事务定稿、DOCX/PDF 和语义查重仍在后续计划中。`codex_interactive` 当前表示准备本地工作目录，不会启动或远程调用 Codex。

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
```

测试全部使用临时目录，不修改真实作品。[CI 模板](docs/ci/offline-regression.yml.example)覆盖 Windows/Linux 和 Python 3.9/3.13；当前 GitHub 令牌缺少 `workflow` 权限，未启用云端 CI。之后由有相应权限的维护者复制到 `.github/workflows/tests.yml` 即可。部分 Windows 环境无符号链接权限时会显示跳过，并另测 junction 边界。

- [设计审计](docs/design_audit_v3.md)：发现、证据与修正。
- [实施计划](docs/implementation_plan_v3.md)：M0–M5 范围、状态及需求追踪。
- [存储契约](docs/storage_contract_v3.md)：来源、指纹、路径与发布边界。
- [原始 v3 设计](docs/VibeNovel_v3_original.md)：原稿留档，其中命令不代表已经实现。
- [v2 使用指南](docs/v2_guide.md)：原模板工作流；三个旧工具继续只处理根目录 v2 作品。

正式小说仍遵循 `AGENTS.md` 和 `prompt.md`：计划批准后写草稿，作者定稿后才更新正式正文与记忆。不要把未经确认的草稿或导入推断当成已发生的事实。
