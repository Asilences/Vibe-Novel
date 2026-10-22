# 参考书研究：关闭工作台后继续运行

Studio 页面是操作入口；付费请求、检查点、原响应、费用记录和研究结果均由常驻后端保存。关闭网页不会停止已经启动的研究。需要查看结果时，打开“资料中心 → 精读与结构图”并选择原工程。

维护程序也可以经 SSH 调用服务器本机的同一组 HTTP 接口。只运行现有后端，不能另开一个指向正式工作区的后端或直接改研究 JSON、账本。接口操作与页面使用相同的模型、来源校验、共享预算、执行器锁和幂等机制；省去页面操作不会降低拆书模型的 Token 费用。

## 接口顺序

请求只发往服务器回环地址。先读取 `GET /api/bootstrap`，将返回的临时 `csrf_token` 放入后续修改请求的 `X-VibeNovel-Token` 请求头，并使用 `Content-Type: application/json`。若发送 `Origin`，必须与请求的回环地址和端口完全一致。此令牌不是模型 API key，不写入 Git，也不输出到共享日志。

| 操作 | 接口 |
| --- | --- |
| 查看私人研究账户 | `GET /api/research-account` |
| 核对旧中断预留 | `POST /api/research-account/attempts/{attempt_id}/reconcile` |
| 保存总预算 | `PUT /api/research-account` |
| 列出／读取研究 | `GET /api/reference-studies`、`GET /api/reference-studies/{id}` |
| 新研究预览／保存 | `POST /api/reference-studies/preview`、`POST /api/reference-studies` |
| 同工程追加剩余正文预览／保存 | `POST /api/reference-studies/{id}/extensions/preview`、`POST /api/reference-studies/{id}/extensions` |
| 中断后的显式续读分支预览／保存 | `POST /api/reference-studies/{id}/continuations/preview`、`POST /api/reference-studies/{id}/continuations` |
| 启动／暂停 | `POST /api/reference-studies/{id}/start`、`POST /api/reference-studies/{id}/pause` |
| 读取／离线修复失败原响应 | `GET`、`PUT /api/reference-studies/{id}/nodes/{node_id}/response` |
| 导出已验证的阅读记录与四组候选图 | `GET /api/reference-studies/{id}/export` |
| 阶段与最终归并预览／保存 | `POST /api/reference-syntheses/preview`、`POST /api/reference-syntheses` |
| 归并控制／结果 | `POST /api/reference-syntheses/{id}/start`、`GET /api/reference-syntheses/{id}/export` |
| 保存个人未采用方法候选 | `POST /api/reference-syntheses/{id}/methods/preview`、`POST /api/reference-syntheses/{id}/methods` |

保存研究、追加范围、归并与方法候选均须使用对应预览 hash 和固定幂等键；成功保存为待启动并不等于已经启动。断线后先读原状态，不能换一个键重建相同付费任务。全书追加只允许同书同版、相同问题及模型的严格原前缀，已发送节点须全部成功。封面、目录、异常与重复候选在预览中单列；“序”等解析为正文的附属介绍仍应在研究缺口中说明。

## 异常与费用

原连续工程因未知请求中断、且模板已更新时，可显式准备续读分支。它逐字节继承成功前缀的结果与检查点，冻结父清单、全部前缀结果hash和原范围；新尾部使用当前模板和模型，旧工程与账本不改。仅无缓存且已核对为unknown的节点可申请另行付费补读，必须有作者明确许可，不能把“继续”当作自动重试授权。请求的`allow_uncertain_reread`须为布尔值，缺少许可时只可预览，禁止保存分支；`uncertain_reread_max_usd`独立限制这些新补读的累计预留，旧未知占用仍计入共享账户。提交另绑定预览hash、固定幂等键和`reason`；同一父工程仅允许一个续读分支。启动、发送及返回发布前重验父工程、缓存、旧账、版本和授权，变化即阻止发布并保留原响应。

运行调度器必须尊重页面暂停和取消，不得把作者暂停状态当作需要自动恢复的异常。关闭网页不等于暂停；作者明确暂停/取消后，保留全部已发送请求，不自动重启该工程。

暂停会等待当前请求落盘后停止新请求。未知费用不会因进程停止、页面关闭、服务器重启或预算提高而清零。旧请求核对先验证归属、冻结请求与执行器锁；有缓存则核算原响应，没有缓存只可标为费用未知并保留预留，不重新发送。

格式或引文校验失败后保留原响应；可以依据实际已发送原文做有说明的离线修复。修复仍须通过完整合同和精确引文校验，不能凭模糊相似度自动认定证据正确，也不能为了继续执行补造分析。达到共享账户或研究批次上限即停止新请求。

全书完成指当前选定版本的有效正文均有成功研究检查点，再完成阶段与最终归并；不代表已核验出版版本完整或模型掌握了所有文学信息。归并只接收有界的阅读观察和已核引文，保留每层来源、被省略项和完整初读结果，不重复付费发送全书原文。方法入库仍是未采用候选，索引仍为可重建缓存；作者显式采用、绑定用途并选入任务后才会用于创作。
