# Zotero + Word 标准交叉引用

## 项目定位

本项目定位为：辅助 coding harness 的论文润色、修改与投稿前预审能力层。
它不宣称、也不得被描述为“无需人工签核的最终发表系统”。

## 已实现能力

- DOCX 中保留 `ADDIN ZOTERO_ITEM CSL_CITATION` 正文引用域；
- 参考文献表使用 `ADDIN ZOTERO_BIBL ... CSL_BIBLIOGRAPHY` 动态域；
- 保留 `ZOTERO_PREF_1/2` 文档偏好，使 Word 插件能识别 IEEE 等引文样式；
- 自动把数字方括号 `[n]` 重绑定为 Zotero 域；
- 审计引用域、文献表域、域闭合状态、文档偏好和 item key；
- 通过 Word COM 调用 `Zotero.ZoteroRefresh`；
- 检查 Zotero 条目 DOI 与当前文献表 DOI 是否一致，阻止“表面已刷新、实际元数据过期”的情况。

## Word 内增删引用

1. 启动 Zotero 桌面端并保持运行。
2. 用 Word 打开最终 Zotero 版 DOCX。
3. 在 Zotero 选项卡使用 `Add/Edit Citation`：
   - 新增引用：在正文位置插入并选择 Zotero 条目；
   - 删除引用：点击已有引用域，在编辑窗口中删除条目；
   - 修改页码、前缀、后缀：必须通过 Zotero 编辑窗口，不直接改域结果文本。
4. 在 Zotero 选项卡点击 `Refresh`。
5. 文献表会按当前正文引用重新排序、增删和更新格式。

不要使用“Unlink Citations”，除非已经生成最终投稿副本。解除域后不能再自动更新。

## coding harness 调用

1. `run({"mode": "manifest", "source": "source-with-zotero.docx", "out": "zotero-manifest.json"})`
   提取编号与 item key 清单。
2. `run({"mode": "rebind", "source": "source-with-zotero.docx", "manifest": "zotero-manifest.json", "target": "polished.docx", "output": "polished-zotero.docx"})`
   将润色/重排后的 DOCX 重新绑定为 Zotero 域。
3. `run({"mode": "refresh", "input": "polished-zotero.docx", "output": "polished-zotero.refreshed.docx"})`
   在 Zotero 正在运行时通过 Word 插件刷新。

如果自动文本变换使用了 `cite-manager` 的 `renumber` 或 `insert`，应先更新
`zotero-manifest.json` 中的编号与条目映射，再执行 `rebind`。缺少映射时必须失败，
不得猜测或伪造 Zotero key。

## 验收门禁

- 每个引用域必须包含 begin / instrText / separate / result / end；
- 文献表域必须恰好一个且完整闭合；
- 必须存在 `ZOTERO_PREF_1/2`；
- Word 打开后字段结果不得出现 `ADDIN ZOTERO_*` 或 `CSL_*` 明文；
- `refresh_ready=true` 才允许执行正式 Zotero Refresh；
- Zotero 未运行时只能完成结构和 Word 字段验收，不能声称已刷新。

## 真实材料说明

真实 Zotero 文档、item key、DOI 映射、交付 DOCX 和渲染结果均属于本地材料，
不上传到公开仓库。公开文档只描述接口、门禁和通用工作流。
