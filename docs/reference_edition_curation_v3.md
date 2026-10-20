# 参考书目录、异常与版本整理（a12）

本模块对私有参考书建立可审阅的派生版本，不修改原件，不自动批准目录或补造缺文，不写小说正式正文/记忆，也不调用模型。

## 使用

在参考书架的单书工具中进入「目录与版本整理」。页面先读取当前解析版，提供正文真实标题拆分线索、重复区段隔离建议，以及孤立NUL的精确删除补丁。建议只是草案。

作者可以分页修改标题/原书卷名、把正文改为附记隔离、按来源Unicode码点拆分、合并相邻项或上下重排。每段显示固定来源chapter_id与半开区间。文本补丁使用精确JSON：`chapter_id/start_char/end_char/expected_text/replacement/reason`；必须与旧原文逐字相符，不能靠章名或相似句子猜位置。

先「预览整理差异」，再「保存候选」。保存不改变当前版。选择候选、查看结构与补丁，勾选核对确认后才发布一个新不可变整理版并可设为当前版。旧版本始终可阅读；旧引文和方法仍固定原版本，不随当前版切换而改绑。

版本区可回查映射、查看隔离/改变/删除区间，或显式切换当前版。这里的结构区段不承诺等于出版自然章；技术分册只用于文件大小管理，不是原书卷。

## 保全与版本合同

- 存储：`library/books/<book_id>/edition-candidates/<candidate_id>.json`；派生版本仍在`editions/<edition_id>/`，其中`edition-map.json`保留直接父版的范围映射。
- 输入方案必须让原版每个可读码点恰好出现一次，不能遗漏、重叠或隐式删除章节；隔离内容也保存在派生版中。只有明确的精确补丁可以改变派生文本。
- 所有方案绑定父版edition及metadata hash、书籍当前hash、授权epoch和转换器版本。预览hash须匹配；同幂等键同方案返回原候选，异方案拒绝。撤回或收紧后再放宽不能复活旧候选。
- 父版本与派生版本各保留原始文件字节。未知坏字节仍保存原始字节位置与hash，派生版继承未解决缺口；调整目录或删NUL不会把解码缺口标为已修复。
- 新版先准备完整原件副本、正文、章节页、定位和映射，再用既有参考书catalog journal发布目录指针。未登记但已落盘的相同整理版可以恢复发布，不能覆写不同内容。
- 候选、映射和单清单各8MiB；章段最多20000，单组成最多1000片段，精确补丁最多1000条。每条补丁旧/新正文最多10000字符。整版正文128MiB，派生入库也检查2GiB语料导入配额。超限拒绝，不截断。
- 映射以码点和精确分段生成：`unchanged`可精确换算，`changed`与`deleted`只供差异回查；`disposition=isolated`表示保留但隔离的内容。跨未覆盖区间返回`ambiguous`，不会根据同名自动建立关系。查询支持正向/反向与分页，映射不会授予旧引用新的有效性。
- 当前映射为**直接父子版本**。多代回查可以逐代查询，不提供忽略中间变化的一步跳转；映射不等于旧研究已经在新文本上重新分析。

## HTTP

- `GET /api/reference-books/{book_id}/edition-audit`
- `GET/POST /api/reference-books/{book_id}/edition-candidates`
- `POST /api/reference-books/{book_id}/edition-candidates/preview`
- `GET /api/reference-books/{book_id}/edition-candidates/{candidate_id}`
- `POST /api/reference-books/{book_id}/edition-candidates/{candidate_id}/publish`
- `POST /api/reference-books/{book_id}/editions/{edition_id}/select`
- `GET /api/reference-books/{book_id}/editions/{edition_id}/map`

API拒绝任意文件路径及额外字段，沿用Studio的Host/Origin/CSRF边界和普通请求体上限。所有操作仅本地。

## 真实资料的本轮结果（2026-10-20）

`tools/audit_reference_editions.py --save-candidates`检查了作者显式提供的16本现有资料，私有结果位于`library/catalogs/reference-curation-a12.json`。先保存3个可核对候选；本轮随后在已授权的资料整理范围逐项核对并发布3个派生版，设为当前版。原始候选及旧版仍保留，发布回执为`library/catalogs/reference-curation-publication-a12.json`。具体处理：

1. 《超能名帅》：孤立NUL按准确位置列出删除补丁，作用仅限未来派生文本；原字节保留。
2. 《冠军之心》：18个去空白全文相同候选均列出来源位置；其中尾部12项与前置终章/后记一组相同，另有中前段6项。提供隔离较早副本、保留后出现位置的待核方案，不按标题臆断整书正确顺序。
3. 《三体II：黑暗森林》：原始3资源项中，大正文351126字符内真实标题为「序章」「上 部 面壁者」「中 部 咒语」「下 部 黑暗森林」，起点分别为0、7815、122033、204348。建立可核对的4结构段拆分方案，另外2资源保留；不声称找回不存在的完整出版章目录。

《剑来》的30处坏字节缺口继续保留未知状态；没有另一本可核对版本时，不生成推测补字。其他文件的书目/目录或附属资源提示亦留在报告，不能据此断言发行版本完整。

## 验证

本模块新增12项离线测试通过：候选不改当前版、原字节/旧版保全、精确删NUL、拆分合并重排隔离、漏文/重叠/伪造旧文本拒绝、未知缺口继承、授权A→B→A失效、文件篡改拦截、孤立已发布版本恢复、正反映射/改变/模糊区间、标题候选、HTTP显式发布与CSRF/严格输入。前端TypeScript检查通过。

备份必须包含候选、版本、原件、映射和catalog journal，不能只复制派生正文。付费研究的实际计费作品路由与预算账本仍按独立存储合同一并保留；本模块不建立跨存储共同事务。
