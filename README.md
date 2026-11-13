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

产物在 `dist\OnlyOne\`（**105.4 MB**）：`OnlyOne.exe` 与 `frontend\dist\`、`data\` 同级。**整个文件夹一起拷走**即可离线运行，不是单 EXE。已实测打包后 API、静态页与 M6/M7 能力可访问。

## 数据与日志

- 数据库：`D:\WorkBuddy\onlyone\data\onlyone.db`（打包形态下在 EXE 同级 `data\`）
- 日志：`D:\WorkBuddy\onlyone\data\onlyone.log`（超过 2MB 轮转成 `.log.1`）
- OCR 字形模板缓存：`data\ocr_templates.npz`（首次使用自动生成，约 1.5 MB）
- 笔记图片附件：`data\attachments\<note_id>\`；全库备份：`data\backups\`

凭据使用 Windows DPAPI 加密，绑定当前 Windows 账户，无需主口令。数据库被拷到别的账户或机器上无法解密；同一账户下的其他程序仍可调用系统解密，请勿在不可信环境运行。**凭据不进全文索引**，列表中账号常显、密码/链接/备注默认掩码，明文仅在逐条查看/复制时按需解密。

## 界面

**暖纸主题 + 左侧一级导航（v0.8）**。颜色统一走 `frontend/src/styles.css` 的 CSS 变量，改色只改令牌，视图层不再写死色值：

| 令牌 | 值 | 用途 |
|---|---|---|
| `--paper` / `--surface` | `#f8f4ec` / `#fffdf9` | 页面底 / 卡片与输入框 |
| `--ink` / `--ink-2` / `--ink-3` | `#26231e` / `#5c564c` / `#6f675b` | 正文 / 次要 / 弱化 |
| `--accent` / `--accent-strong` | `#bf5a36` / `#a44524` | 强调装饰 / 强调文字与命中高亮 |
| `--info` / `--night` | `#3e6288` / `#1f3040` | 信息与链接 / 主按钮 |

导航分「知识 / 业务 / 系统」三组（208px），底部显示运行形态与版本。橙色只做强调，主按钮用深墨蓝，避免满屏橙色。参考站原色中 `#8a8174`（3.50:1）与 `#bf5a36` 作正文（4.05:1）不达 AA，已分别校正为 `#6f675b`（5.08:1）与 `#a44524`（5.54:1）。完整规范见 `docs/设计方案.md` §8。

截图：`docs/screenshots/` — 01 全局搜索、02 项目事项、03 凭据库、04 索引目录、05 语义检索、06 记事本。

## 检索能力

- **字面检索**：FTS5 trigram + 短词 LIKE 兜底。
- **扫描件 OCR**：图片与无文本层 PDF 的内嵌页图走本机离线模板匹配引擎（Pillow + 系统中文字体 + numpy），零云端依赖。**能力边界**：适用于清晰印刷体，对手写 / 艺术字体 / 强噪声效果有限；检测到 tesseract 时自动优先使用。
- **语义检索**：本地确定性稀疏向量（同义词归一 + 字符 n-gram + 余弦），非神经嵌入；结果在「语义相近」独立分组。
- **外部索引插件**：`SearchBackend` 抽象默认关闭；仅当 `ONLYONE_KENSAKU_CLI` 指向真实 CLI 时启用，结果独立分组、不参与跨类型关联。

## 记事本（M7，B 档）

- 三级结构「笔记本 → 分区 → 页面」；TipTap 3 富文本编辑器（粗体 / 斜体 / 标题 / 列表 / 代码块 / 图片）。
- 改动后 1 秒防抖自动保存，带 `revision` 乐观锁；页面级「不参与检索」开关（私密页不进 FTS / 语义 / 全局搜索）。
- 公开页面正文自动进入全局检索、语义分组与「关联到项目」。
- 图片落盘到 `data/attachments/`，不进 FTS；导出 MD / HTML / HTML+图片 zip，另有全库 checkpoint 备份。
- **凭据导出只做 C 方案**：Scrypt + AES-GCM 口令加密，导出文件只含密文；导入时用当前 Windows 账户 DPAPI 重新加密，因此**换机也能恢复**。日常使用仍免主口令，口令只在导出 / 恢复时使用。不做明文导出（与设计方案 §10 冲突）。

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
py -3 tools\verify_notes.py   # M7：三级笔记 / 自动保存 / 图片 / 导出 / 凭据加密恢复
py -3 tools\verify_m8.py      # M8：后台扫描进度 / 并发写 / pptx 表格 / 系统目录排除
py -3 tools\capture_ui.py     # Chrome headless 真实界面截图 → docs\screenshots\
```

> `verify_notes.py` 使用独立临时数据目录（`ONLYONE_DATA_DIR`），不会读写 `data\onlyone.db`。

## 当前进度

**v1.0 范围基线（M1–M5）、用户纳入的 M6 三项增强、M7 记事本、M8 扫描体验与缺陷修复、v0.8 界面重构全部完成。** 剩余为技术债与可选优化，见 `docs\续接-未完成项.md` §3 G 表。

- M1 后端壳 / 文件名检索；M2 Office 正文抽取与只读预览；M3 四类业务记录与关联；M4 DPAPI 凭据库；M5 增量索引、失败清单、向导、打包、真实截图；M6 OCR / 语义检索 / 可插拔外部索引；M7 富文本记事本 + 导出备份 + 凭据加密迁移；M8 后台扫描进度条、分块提交消除写锁冲突、pptx 表格修复、系统目录剪枝；v0.8 暖纸主题与左侧导航。

## 续接

新会话请先读 `docs\续接-未完成项.md`：各阶段落地细节、能力边界、技术债、环境事实、常用命令与决策记录都在里面。
