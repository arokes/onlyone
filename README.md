# OnlyOne 个人工作台

单机、单人、离线的本地工作台：统一检索本地 Word/Excel/PPT、扫描件、爱数同步盘目录、企微在线文档链接、富文本记事本、工作日志与运维问题记录，并在本机保存账号密码。

技术栈对齐 ACMNG 的架构（FastAPI + pywebview + Vue 3 + Naive UI），代码独立编写。设计见 `docs/设计方案.md`，真实界面截图见 `docs/screenshots/`。界面为**暖纸主题 + 左侧一级导航**，配色规范见设计方案 §8。

## 运行

开发期两个进程：

```powershell
cd D:\WorkBuddy\onlyone\backend
py -3 run.py --no-window --port 8765

cd D:\WorkBuddy\onlyone\frontend
npm install
npm run dev
```

生产形态（单进程窗口，自动启用文件监听）：

```powershell
cd D:\WorkBuddy\onlyone\frontend
npm run build
cd ..\backend
py -3 run.py
```

`run.py` 随机选空闲端口、生成一次性令牌（仅本进程有效），前端从 URL 取令牌后放进 `X-OnlyOne-Token` 请求头。API 只监听 `127.0.0.1`。桌面窗口创建失败时自动降级到系统默认浏览器，也可手动加 `--browser`。

## 打包（文件夹交付）

```powershell
cd D:\WorkBuddy\onlyone
py -3 build.py
```

产物在 `dist\OnlyOne\`（**111.2 MB**）：`OnlyOne.exe` 与 `frontend\dist\`、`data\` 同级。**整个文件夹一起拷走**即可离线运行，不是单 EXE。

`OnlyOne.exe` 用 `--noconsole` 打包（PE Subsystem=2），**启动不再弹命令行窗口**；代价是没有控制台输出，所有异常写 `data\onlyone.log`（`run.py` 已做 `sys.stdout is None` 兜底，并把 uvicorn 自带日志配置关掉）。需要另存产物目录时用 `py -3 build.py --dist-dir dist_next`，可保留旧产物不被覆盖。

## 数据与日志

- 数据库：`D:\WorkBuddy\onlyone\data\onlyone.db`（打包形态下在 EXE 同级 `data\`）
- 日志：`D:\WorkBuddy\onlyone\data\onlyone.log`（超过 2MB 轮转成 `.log.1`）
- OCR 字形模板缓存：`data\ocr_templates.npz`（首次使用自动生成，约 1.5 MB）
- 笔记图片附件：`data\attachments\<note_id>\`；全库备份：`data\backups\`

凭据使用 Windows DPAPI 加密，绑定当前 Windows 账户，无需主口令。数据库被拷到别的账户或机器上无法解密；同一账户下的其他程序仍可调用系统解密，请勿在不可信环境运行。**凭据不进全文索引**，列表中账号/链接/备注常显、**密码默认掩码**（点该行密码列的眼睛图标才展开，刷新或离开页签立即恢复）；编辑弹窗的密码框不再回填明文，留空即「不修改」。

### 索引库体积维护

`fts_entries` 是 FTS5 trigram 索引：条目更新都是 `DELETE` + `INSERT`，死段与空闲页不会自动归还。
实测 1367 条条目时库文件 309 MB，其中有效数据只有 108 MB。维护手段有两级：

| 方式 | 触发 | 效果 |
|---|---|---|
| 自动 | 每次扫描任务结束后，空闲页 ≥16 MB 且距上次 ≥1 小时 | 后台线程做 `optimize` + `VACUUM`，实测 109.8→108.9 MB，约 4 秒 |
| 手动 | `py -3 tools/db_maintenance.py`（**先退出 OnlyOne**） | `optimize` + `VACUUM`，实测 309.1→108.1 MB（回收 65%），6 秒 |

阈值可用环境变量覆盖：`ONLYONE_COMPACT_MIN_FREE_PAGES`（默认 4000 页 = 16 MB）、
`ONLYONE_COMPACT_MIN_INTERVAL_SEC`（默认 3600 秒；`0` 表示每次扫描后都做）。
`--no-vacuum` 只合并索引段不动磁盘；`--dry-run` 只打印指标。
压缩前后检索结果与条目数不变（`integrity_check` + FTS `integrity-check` 已校验）。

## 界面

**暖纸主题 + 左侧一级导航（v0.8）**。颜色统一走 `frontend/src/styles.css` 的 CSS 变量，改色只改令牌，视图层不再写死色值：

| 令牌 | 值 | 用途 |
|---|---|---|
| `--paper` / `--surface` | `#f8f4ec` / `#fffdf9` | 页面底 / 卡片与输入框 |
| `--ink` / `--ink-2` / `--ink-3` | `#26231e` / `#5c564c` / `#6f675b` | 正文 / 次要 / 弱化 |
| `--accent` / `--accent-strong` | `#bf5a36` / `#a44524` | 强调装饰 / 强调文字与命中高亮 |
| `--info` / `--night` | `#3e6288` / `#1f3040` | 信息与链接 / 主按钮 |

导航分「知识 / 业务 / 系统」三组（208px），底部显示运行形态与版本。橙色只做强调，主按钮用深墨蓝，避免满屏橙色。参考站原色中 `#8a8174`（3.50:1）与 `#bf5a36` 作正文（4.05:1）不达 AA，已分别校正为 `#6f675b`（5.08:1）与 `#a44524`（5.54:1）。完整规范见 `docs/设计方案.md` §8。

**项目事项（v0.9）= 项目工作台**：左侧项目列表（搜索 + 进行中/已归档过滤），右侧四个页签 —— 企微链接 / 运维记录 / 工作日志 / 关联文件，页签标题带数量徽标；业务页签沿用统一的表格组件（新增 / 编辑 / 批量删除 / TSV 复制），归属项目自动锁定为当前项目，不再需要点抽屉。**记事本不再出现在项目页**（2026-11-17 决定，只撤 UI）：早先由项目页新建的页面仍落在「项目笔记」笔记本下按项目名命名的分区，`notes.project_id` 保留，在记事本里照旧可看。

**Word 内嵌对象（v0.9）**：`extractor` 会读取 docx 里 `word/embeddings/` 下的内嵌 Excel —— 直接嵌入的 `.xlsx` 部件、OLE `package` 流、老式 `Workbook` / `Book` 流，以及 `Ole10Native` 标准包。内嵌位图（Excel 截图）按决定不做 OCR。索引正文上限从 10 万字提到 20 万字，避免多工作表文档尾部被整段截断。

截图：`docs/screenshots/` — 01 全局搜索、02 项目事项、03 凭据库、04 索引目录、05 语义检索、06 记事本、09 记事本表格。

## 检索能力

- **字面检索**：FTS5 trigram + 短词 LIKE 兜底。
- **扫描件 OCR**：图片与无文本层 PDF 的内嵌页图走本机离线模板匹配引擎（Pillow + 系统中文字体 + numpy），零云端依赖。**能力边界**：适用于清晰印刷体，对手写 / 艺术字体 / 强噪声效果有限；检测到 tesseract 时自动优先使用。
- **语义检索**：本地确定性稀疏向量（同义词归一 + 字符 n-gram + 余弦），非神经嵌入；结果在「语义相近」独立分组。
- **外部索引插件**：`SearchBackend` 抽象默认关闭；仅当 `ONLYONE_KENSAKU_CLI` 指向真实 CLI 时启用，结果独立分组、不参与跨类型关联。

## 只读预览

- 正文默认 80 行、表格默认 31 行（含表头、表头粘性固定），宽表可「放大查看」到 92vw 弹窗并分批加载；抽屉宽度可拖拽，标题栏可一键最大化。
- **内嵌图片可见**：枚举 `word/media`、`ppt/media`、`xl/media`；缩略图网格收在文末折叠区（默认收起），单击看大图、可上一张/下一张、点击图片在「适应窗口 / 原始尺寸」间切换。
- **原位显示**：正文里的图直接在原文位置渲染——独占一段的居中出大图（≤420px，下方标注「图 N · 宽 × 高」），夹在句子中的出 180px 行内小图，表格单元格里出 160px 小图；点任一张开大图。滚动到才取字节（懒加载）。
- EMF/WMF 矢量图经 Pillow（Windows GDI）栅格化为 PNG，实测流程图文字清晰；`.wdp`/`.svg`/WPS 私有扩展名不可预览，直接不列出。**图片不进全文索引**，图内文字仍需 OCR 才能搜到。原位位置是**段落级近似**（浮动图会被拉到所在段落），且只对 `.docx` 生效。

## 记事本（M7，B 档）

- 三级结构「笔记本 → 分区 → 页面」；TipTap 3 富文本编辑器（粗体 / 斜体 / 标题 / 列表 / 代码块 / 图片 / 表格）。
- **布局**：笔记本下拉 + 分区页签固定在顶部（页签本身不滚动，放不下的收进「更多 ▾」）；左侧是当前分区的页面列表、右侧是编辑器，两栏各自独立滚动，整个页面不再整体滚动。
- 改动后 1 秒防抖自动保存，带 `revision` 乐观锁；页面级「不参与检索」开关（私密页不进 FTS / 语义 / 全局搜索）。
- 公开页面正文自动进入全局检索、语义分组与「关联到项目」。
- 图片落盘到 `data/attachments/`，不进 FTS；导出 MD / HTML / HTML+图片 zip，另有全库 checkpoint 备份。
- **表格**：工具栏「表格」菜单支持插入 3×3、增删行列、合并 / 拆分单元格、删整表；光标不在表内时行列类菜单项置灰。从 Excel / 网页复制可直接 Ctrl+V 粘贴成表格。导出 Markdown 时合并单元格按 GFM 拍平（内容保留、结构丢失，Markdown 无此语义），导出 HTML / zip 则保留 `colspan` / `rowspan`。
- **凭据导出只做 C 方案**：Scrypt + AES-GCM 口令加密，导出文件只含密文；导入时用当前 Windows 账户 DPAPI 重新加密，因此**换机也能恢复**。日常使用仍免主口令，口令只在导出 / 恢复时使用。不做明文导出（与设计方案 §10 冲突）。

## 从 OneNote 迁移

OneNote 的 `.onepkg` / `.one` 是 MS-ONESTORE 二进制格式，本机 OneNote 的 COM 类型库未注册（`0x8002801D 库没有注册`，Excel/Word 的 COM 正常），所以走 **OneNote 手动导出 `.mht` + 导入** 这条路。

**方式一：界面导入（推荐）**——记事本左侧树「分区」行 → **导入** → 选「.mht 文件（可多选）」或「选择文件夹」。

- 一个 `.mht` = 一个分区，分区名取文件名；导入到**当前选中的笔记本**
- 逐个文件串行上传，显示 `正在导入 2/5：MM.MHT`；完成后弹窗汇总分区 / 页面 / 图片数
- 重复导入同一节自动生成「MM (2)」，不会把页面混进旧分区
- 单文件上限 64 MB，单次最多 20 个文件

**方式二：命令行**（默认只分析、不写库）：

```powershell
py -3 tools\import_mht_notes.py --input "D:\1118\MM.MHT"                  # 单节，只看报告
py -3 tools\import_mht_notes.py --input "D:\1118\SAP\" --write --book "SAP 笔记"   # 整本目录
```

OneNote 端的操作：**文件 → 导出 → 范围选「节」或「笔记本」→ 格式「单个文件网页 (.mht)」**。

映射：**笔记本 → OnlyOne 笔记本**，**每个 `.mht` → 一个分区**，**节内每页 → 一个页面**。

保真度：页面标题、正文段落、表格（含 colspan/rowspan）、图片（压缩 + 哈希落盘，位置保留）、粗体/斜体、列表都保留；**子页面缩进、手写墨迹、内嵌非图片附件丢失**，`onenote:#...` 站内链接降级为纯文字。**整本导出为单个 .mht 时节边界不存在**（实测 177 页页间标记完全一致），整本只能进一个分区。

## 回归

七个脚本各自拉起后端，无需手动启动服务；**必须串行运行，不要并发**（共用同一个 SQLite 库）。

```powershell
cd D:\WorkBuddy\onlyone
py -3 tools\smoke_test.py     # M1：状态 / 根目录 / 扫描 / 中文检索 / DPAPI
py -3 tools\verify_m2.py      # M2：真实 Office 文件端到端，约 30–60s（含 WPS COM）
py -3 tools\verify_m3.py      # M3：业务记录 / 关联 / 跨类型检索
py -3 tools\verify_m4.py      # M4：凭据 / 明文泄漏 / 敏感排除
py -3 tools\verify_m5.py      # M5：watcher / 失败队列 / 重建 / EXE 离线启动
py -3 tools\verify_m6.py      # M6：OCR / 语义相近 / 外部索引插件
py -3 tools\verify_notes.py   # M7：三级笔记 / 自动保存 / 图片 / 表格导出 / 凭据加密恢复
node tools\verify_note_table_ui.mjs  # 记事本表格界面：插入 / 合并 / 自动保存（CDP，需先 npm run build）
node tools\verify_note_import_ui.mjs # 记事本导入界面：文件 / 文件夹选择 → 分区落库（CDP，自包含样本）
node tools\verify_note_layout.mjs     # 记事本布局：顶部页签固定 / 左右独立滚动 /「更多」收纳（CDP）
py -3 tools\import_mht_notes.py --input <某个.mht>   # OneNote 迁移试跑（不加 --write 不落库）
py -3 tools\verify_m8.py      # M8：后台扫描进度 / 并发写 / pptx 表格 / 系统目录排除
py -3 tools\capture_ui.py     # Chrome headless 真实界面截图 → docs\screenshots\
```

> `verify_notes.py` 使用独立临时数据目录（`ONLYONE_DATA_DIR`），不会读写 `data\onlyone.db`。
> `verify_m8.py` 目前仍会在「写请求 <10s」一条失败：强制重建期间写请求实测阻塞 12–19 秒（G-20），
> 其余 8 条断言通过。属产品侧提交粒度问题，不是索引维护引入的。

## 当前进度

**v1.0 范围基线（M1–M5）、用户纳入的 M6 三项增强、M7 记事本、M8 扫描体验与缺陷修复、v0.8 界面重构全部完成。** 剩余为技术债与可选优化，见 `docs\续接-未完成项.md` §3 G 表。

- M1 后端壳 / 文件名检索；M2 Office 正文抽取与只读预览；M3 四类业务记录与关联；M4 DPAPI 凭据库；M5 增量索引、失败清单、向导、打包、真实截图；M6 OCR / 语义检索 / 可插拔外部索引；M7 富文本记事本 + 导出备份 + 凭据加密迁移；M8 后台扫描进度条、分块提交消除写锁冲突、pptx 表格修复、系统目录剪枝；v0.8 暖纸主题与左侧导航。

## 续接

新会话请先读 `docs\续接-未完成项.md`：各阶段落地细节、能力边界、技术债、环境事实、常用命令与决策记录都在里面。
