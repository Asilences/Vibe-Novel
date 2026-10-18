# 本机工作环境与 GitHub 操作约定

更新：2026-10-19。作者已提供以下配置，后续在本机工作时直接复用，不重复索取。

- 仓库：`https://github.com/Asilences/Vibe-Novel.git`。
- VPN / 本机代理端口：**7890**；Git 与 GitHub API 使用 `http://127.0.0.1:7890`。
- GitHub 令牌：保存于本机 `config/github.local.json` 的 `token` 字段；作者说明已授予 `repo`、`workflow`。实际有效性以认证检查为准。
- 2026-10-19 已通过 GitHub 只读认证检查：账户 `Asilences`，服务端确认权限 `repo, workflow`。后续无需再次索取已有配置。
- 该配置文件及其备份已由 `/config/*.local.json*` 排除 Git。工作文档只记录凭据位置与使用方式，禁止把令牌正文写入提交、PR、URL、日志或聊天回复。
- 这份凭据仅用于上述仓库的 GitHub 操作，不是 Studio 的模型 API key；后者使用独立的 `config/api.local.json`。
- Git 提交信息、PR 标题和正文使用中文。源码可以按本轮开发任务需要上传；正式小说审批边界仍按根规则执行。

## 使用已保存的配置

优先执行现有包装脚本，由脚本内部读取凭据，不在终端中输出配置全文，也不把令牌放进命令参数：

```powershell
.venv/Scripts/python.exe tools/github_local.py check
.venv/Scripts/python.exe tools/github_local.py fetch origin
.venv/Scripts/python.exe tools/github_local.py push -u origin <当前分支>
```

脚本只为子进程注入代理及 GitHub 认证，不触发 Git 登录对话框，不把令牌写入 Git remote 或全局 Git 配置。仅支持本仓库的 fetch、push、ls-remote 与只读认证检查。

后续需要 GitHub REST API 时，同样在执行脚本内部读取本地配置，通过请求头认证，代理使用上面的固定地址；仅报告 HTTP 状态与必要的非敏感结果。不要因为连接器写权限不足而重新询问已有令牌。只有本地凭据缺失、失效或作者明确更换时，才说明具体问题。
