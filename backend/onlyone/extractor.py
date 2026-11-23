# -*- coding: utf-8 -*-
"""统一正文抽取器（M2；M5-7 增加 WPS 会话复用）。

对外只暴露 ``extract(path) -> ExtractResult(text, tables, error)``：

* 新格式与纯文本走纯 Python 库；未知后缀不算错误，只返回空正文（仍索引文件名）。
* ``.doc`` / ``.ppt`` 走 WPS COM 另存为 OOXML 到临时目录，再复用纯 Python 抽取。
* 任何异常都收敛为 ``error`` 字符串，绝不向扫描器抛出，保证一次扫描不中断。
* ``wps_session()`` 让同一次扫描复用 WPS 实例（每个线程各持一套），避免逐文件启停。

WPS COM 关键实测（2026-11-06，本机 WPS 10.8.2.6837 / COM 12.0）：

* 可用 ProgID：``KWPS.Application`` / ``KWPP.Application`` / ``KET.Application``，
  以及 WPS 注册的兼容名 ``Word.Application`` / ``PowerPoint.Application`` / ``Excel.Application``。
* Writer 另存 ``.docx`` **必须 FileFormat=12**（wdFormatXMLDocument）。
  FileFormat=16 会「调用成功但产物仍是 OLE2 的 .doc」，python-docx 打不开。
* PPT 另存 ``.pptx`` 用 FileFormat=24（ppSaveAsOpenXMLPresentation）。
* 不传 FileFormat 时 WPS 按原格式保存，扩展名会被无视。
* 因此每次转换后**必须校验产物前 4 字节为 ``PK\\x03\\x04``**，否则按失败处理。
* 备用路径（暂未启用）：``Document.ExportAsFixedFormat(pdf, 17)`` 可导出 PDF，pypdf 可读中文。
"""
from __future__ import annotations

import io
import logging
import struct
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("onlyone")

MAX_TEXT_CHARS = 2_000_000
# 2M 是「单文件正文总预算」，不再是单条索的上限：正文按 CHUNK_CHARS 分片写入同一条目的
# 多行 fts_entries，检索按 entry_id 汇总，因此不再出现「截断即丢内容」。
# 实测最大单文件 136.5 万字（ZPP044-185000021-VC.XLSX，3 表 × 2000 行 × 40 列）；
# 200k 时本机有 43 个条目恰好卡在上限（用户角色权限（25年版）.xlsx 34.7 万字被截掉三张表）。
CHUNK_CHARS = 200_000
MAX_TABLE_ROWS = 2_000
MAX_TABLE_COLS = 40
MAX_TABLES = 20
# 宽表溢出列上限：表格结构只保留前 MAX_TABLE_COLS 列（只读预览不易过宽），
# 第 41 列之后的内容以纯文本行进正文，避免「一人一列」的权限矩阵整列丢失。
# 256 而非 16384：openpyxl read_only 模式下部分工作簿的 dimension 虚高到 16384 列，
# 不设上限会让每行多扫一万多个空单元格。
WIDE_MAX_COLS = 256
MAX_PDF_PAGES = 500

# OOXML 命名空间：只为在预览里定位图片（a:blip / v:imagedata）而用
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_V = "urn:schemas-microsoft-com:vml"
_W_T = "{%s}t" % _W
_W_TAB = "{%s}tab" % _W
_W_BR = "{%s}br" % _W
_A_BLIP = "{%s}blip" % _A
_R_EMBED = "{%s}embed" % _R
_V_IMAGEDATA = "{%s}imagedata" % _V
_R_ID = "{%s}id" % _R

TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".ini", ".yaml", ".yml"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_EXTS = {
    ".docx", ".xlsx", ".pptx", ".xls", ".pdf", ".doc", ".ppt",
} | TEXT_EXTS | IMAGE_EXTS
WPS_EXTS = {".doc": "word", ".ppt": "ppt"}

# COM 自动化不能在多线程并发调用；FastAPI 的同步路由跑在线程池里，
# 因此用全局锁串行化，并在每次调用所在线程显式 CoInitialize（STA）。
_COM_LOCK = threading.Lock()
# 每个线程持有自己的 WPS COM 实例集合（COM 单元不能跨线程复用）。
_local = threading.local()


@contextmanager
def wps_session():
    """在本次扫描期间复用 WPS 实例（M5-7），避免每个旧格式文件都启停一次进程。

    可重入：嵌套调用只在最外层做 CoInitialize / Quit / CoUninitialize。
    全局 ``_COM_LOCK`` 保证跨线程不会并发进入 COM 调用。
    """
    import pythoncom

    depth = getattr(_local, "depth", 0)
    if depth == 0:
        pythoncom.CoInitialize()
        _local.apps = {}
    _local.depth = depth + 1
    try:
        yield
    finally:
        _local.depth -= 1
        if _local.depth == 0:
            for app in list(_local.apps.values()):
                try:
                    app.Quit()
                except Exception:  # noqa: BLE001
                    pass
            _local.apps = {}
            pythoncom.CoUninitialize()


_WPS_PROGIDS = {
    "word": ("KWPS.Application", "Word.Application"),
    "ppt": ("KWPP.Application", "PowerPoint.Application"),
}
_WPS_FORMATS = {"word": 12, "ppt": 24}
_WPS_DEST_EXT = {"word": ".docx", "ppt": ".pptx"}


@dataclass
class ExtractResult:
    """一次抽取的结果；``error`` 非空表示失败（扫描器仍会索引文件名）。"""

    text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def body(self) -> str:
        """写入 fts_entries.body 的可检索文本：正文 + 表格文本化。"""
        parts: list[str] = [self.text] if self.text else []
        for table in self.tables:
            for row in table:
                line = " ".join(cell for cell in row if cell).strip()
                if line:
                    parts.append(line)
        return "\n".join(parts)[:MAX_TEXT_CHARS]


def _clip(text: str) -> str:
    return (text or "").strip()[:MAX_TEXT_CHARS]


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """把正文切成每片不超过 ``size`` 的若干段，优先在换行处断开。

    分片写入同一条目的多行 ``fts_entries``，检索结果按 entry_id 汇总。
    这样单文件不再受单条正文长度限制，同时又不会让单行过大拖慢 FTS 与向量化。
    """
    text = text or ""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    used = 0
    for line in text.split("\n"):
        # 无换行的超长单行（压缩 JSON / 单行 CSV）按 size 硬切
        while len(line) > size:
            if buf:
                chunks.append("\n".join(buf))
                buf, used = [], 0
            chunks.append(line[:size])
            line = line[size:]
        if buf and used + len(line) + 1 > size:
            chunks.append("\n".join(buf))
            buf, used = [], 0
        buf.append(line)
        used += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _wide_row_text(row) -> str:
    """一行中第 MAX_TABLE_COLS 列之后的非空文本。

    表格结构（只读预览）仍只保留前 40 列，但溢出列必须进正文：
    权限矩阵类工作簿是「一人一列」，丢掉第 41 列之后等于整片人员搜不到。
    """
    if len(row) <= MAX_TABLE_COLS:
        return ""
    return " ".join(
        s for s in (("" if v is None else str(v)).strip()
                    for v in row[MAX_TABLE_COLS:WIDE_MAX_COLS]) if s
    )


def _docx_para_text(el, rel_map: dict[str, int]) -> str:
    """按 XML 文档顺序取段落文本，并在图片出现的位置插入 ``[图 N]`` 标记。

    只在只读预览（``with_images=True``）时启用，扫描入库仍走 ``p.text``，
    避免图片标记污染全文索引。
    """
    parts: list[str] = []
    last: int | None = None
    for node in el.iter():
        tag = node.tag
        if tag == _W_T:
            parts.append(node.text or "")
        elif tag == _W_TAB or tag == _W_BR:
            parts.append(" ")
        elif tag == _A_BLIP or tag == _V_IMAGEDATA:
            rid = node.get(_R_EMBED if tag == _A_BLIP else _R_ID)
            idx = rel_map.get(rid) if rid else None
            # mc:AlternateContent 的 Choice/Fallback 会重复指向同一张图，去重
            if idx is not None and idx != last:
                parts.append(" [图 %d] " % (idx + 1))
                last = idx
    return "".join(parts)


def _docx_body(doc, rel_map: dict[str, int] | None = None
               ) -> tuple[list[str], list[list[list[str]]]]:
    """python-docx 文档对象 →（段落行, 表格）。不含内嵌对象。

    ``rel_map`` 非空时在正文与表格单元格里插入 ``[图 N]`` 标记（只读预览用）。
    """
    if rel_map:
        lines = [t for t in (x.strip() for x in
                             (_docx_para_text(p._element, rel_map) for p in doc.paragraphs))
                 if t]
    else:
        lines = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    tables: list[list[list[str]]] = []
    for table in doc.tables[:MAX_TABLES]:
        rows: list[list[str]] = []
        for row in table.rows[:MAX_TABLE_ROWS]:
            cells: list[str] = []
            for cell in row.cells[:MAX_TABLE_COLS]:
                if rel_map:
                    # 图片常在单元格里（截图贴在表格中），必须逐段落拼才拿得到标记
                    text = " ".join(
                        t for t in (x.strip() for x in
                                    (_docx_para_text(p._element, rel_map)
                                     for p in cell.paragraphs)) if t)
                else:
                    text = cell.text
                cells.append(text.strip().replace("\n", " "))
            rows.append(cells)
        if rows:
            tables.append(rows)
    return lines, tables


def _from_docx(path: Path, with_images: bool = False) -> ExtractResult:
    """正文 + 表格 + ``word/embeddings/`` 下的内嵌对象（Excel / Word）。

    内嵌 Excel 在 Word 里有三种落盘形态，全部在这里处理：
    ① 直接嵌入的 OOXML 部件（``Microsoft_Excel_Worksheet.xlsx``）；
    ② OLE 复合包里的 ``package`` 流（OLE Package，内容仍是 xlsx/xls）；
    ③ OLE 复合包里的 ``Workbook`` / ``Book`` 流（老式嵌入工作簿）。
    内嵌位图（PBrush，即 Excel 截图）按用户决定不做 OCR，直接跳过——
    只读预览改由 ``word/media/`` 里的预览图展示（Word 必存一张）。

    ``with_images=True`` 时在正文/单元格图片位置插 ``[图 N]``（仅预览用，
    扫描入库走默认值 False，标记不进全文索引）。
    """
    from docx import Document

    rel_map: dict[str, int] | None = None
    if with_images:
        try:
            from .images import docx_rel_map

            rel_map = docx_rel_map(path) or None
        except Exception as exc:  # noqa: BLE001 - 图片标记失败不该影响正文
            log.debug("图片标记跳过 %s: %s", path, exc)

    doc = Document(str(path))
    lines, tables = _docx_body(doc, rel_map)
    emb_lines, emb_tables = _docx_embeddings(path)
    return ExtractResult(_clip("\n".join(lines + emb_lines)),
                         (tables + emb_tables)[:MAX_TABLES])


def _from_xlsx(path: Path) -> ExtractResult:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    try:
        for ws in wb.worksheets:
            lines.append("工作表：" + ws.title)
            rows: list[list[str]] = []
            for idx, row in enumerate(ws.iter_rows(values_only=True)):
                if idx >= MAX_TABLE_ROWS:
                    break
                cells = ["" if v is None else str(v) for v in row[:MAX_TABLE_COLS]]
                if any(c.strip() for c in cells):
                    rows.append(cells)
                # 页宽超过 40 列时，溢出列以纯文本补进正文（不入表格结构）
                extra = _wide_row_text(row)
                if extra:
                    lines.append(extra)
            if rows:
                tables.append(rows)
            if len(tables) >= MAX_TABLES:
                break
    finally:
        wb.close()
    return ExtractResult(_clip("\n".join(lines)), tables)


def _from_pptx(path: Path) -> ExtractResult:
    from pptx import Presentation

    prs = Presentation(str(path))
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    for idx, slide in enumerate(prs.slides, 1):
        lines.append("第 %d 页" % idx)
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text.strip():
                lines.append(shape.text.strip())
            if getattr(shape, "has_table", False) and len(tables) < MAX_TABLES:
                # python-pptx 的 _CellCollection 只实现 __getitem__(int)，传 slice 会抛
                # TypeError: '<' not supported between 'slice' and 'int'，必须先转 list。
                rows = [
                    [cell.text.strip().replace("\n", " ")
                     for cell in list(row.cells)[:MAX_TABLE_COLS]]
                    for row in list(shape.table.rows)[:MAX_TABLE_ROWS]
                ]
                if rows:
                    tables.append(rows)
    return ExtractResult(_clip("\n".join(lines)), tables)


def _from_xls(path: Path) -> ExtractResult:
    import xlrd

    book = xlrd.open_workbook(str(path))
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    for sheet in book.sheets():
        lines.append("工作表：" + sheet.name)
        rows: list[list[str]] = []
        for r in range(min(sheet.nrows, MAX_TABLE_ROWS)):
            values = [sheet.cell_value(r, c)
                      for c in range(min(sheet.ncols, WIDE_MAX_COLS))]
            # 表格结构与改造前一致：不去空行，只补溢出列正文
            rows.append([str(v) for v in values[:MAX_TABLE_COLS]])
            extra = _wide_row_text(values)
            if extra:
                lines.append(extra)
        if rows:
            tables.append(rows)
        if len(tables) >= MAX_TABLES:
            break
    return ExtractResult(_clip("\n".join(lines)), tables)


# ------------------------------------------------------------ 内嵌对象（docx 里的 Excel）

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
MAX_EMBED_OBJECTS = 40                      # 单个 docx 最多处理的内嵌对象数
MAX_EMBED_BYTES = 60 * 1024 * 1024          # 单个内嵌对象大小上限
MAX_EMBED_DEPTH = 2                         # 内嵌文档再内嵌的递归层数


def _xlsx_bytes_tables(data: bytes) -> tuple[list[str], list[list[list[str]]]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets: list[str] = []
    tables: list[list[list[str]]] = []
    try:
        for ws in wb.worksheets:
            sheets.append(ws.title)
            rows: list[list[str]] = []
            for idx, row in enumerate(ws.iter_rows(values_only=True)):
                if idx >= MAX_TABLE_ROWS:
                    break
                cells = ["" if v is None else str(v) for v in row[:MAX_TABLE_COLS]]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            if rows:
                tables.append(rows)
            if len(tables) >= MAX_TABLES:
                break
    finally:
        wb.close()
    return sheets, tables


def _xls_bytes_tables(data: bytes) -> tuple[list[str], list[list[list[str]]]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sheets: list[str] = []
    tables: list[list[list[str]]] = []
    for sheet in book.sheets():
        sheets.append(sheet.name)
        rows = [
            [str(sheet.cell_value(r, c)) for c in range(min(sheet.ncols, MAX_TABLE_COLS))]
            for r in range(min(sheet.nrows, MAX_TABLE_ROWS))
        ]
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if rows:
            tables.append(rows)
        if len(tables) >= MAX_TABLES:
            break
    return sheets, tables


def _read_cstr(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.find(b"\x00", pos)
    if end < 0 or end - pos > 512:
        raise ValueError("no cstring")
    raw = buf[pos:end]
    try:
        text = raw.decode("mbcs")
    except Exception:  # noqa: BLE001 - 非 Windows 或非 ANSI 字符
        text = raw.decode("latin-1", "replace")
    return text, end + 1


def _is_document(data: bytes) -> bool:
    return data[:4] == _ZIP_MAGIC or data[:8] == _OLE_MAGIC


def _ole10_payload(raw: bytes) -> tuple[str, bytes]:
    """从 ``\\x01Ole10Native`` 流里取原始内嵌文件；解不出返回 ``("", b"")``。

    变体 A 是 Word 标准打包（4B 总长 + 2B flags + label + 文件名 + 临时路径 + 4B 长度 + 数据）；
    变体 B 是 4B 长度 + 裸数据（PBrush 截图与部分 WPS 产物如此），此时数据不是文档。
    """
    try:
        pos = 6
        _label, pos = _read_cstr(raw, pos)
        name, pos = _read_cstr(raw, pos)
        _tmp, pos = _read_cstr(raw, pos)
        size = struct.unpack_from("<I", raw, pos)[0]
        body = raw[pos + 4:pos + 4 + size]
        if _is_document(body):
            return name, body
    except Exception:  # noqa: BLE001 - 落到变体 B
        pass
    if len(raw) >= 4:
        size = struct.unpack_from("<I", raw, 0)[0]
        body = raw[4:4 + size]
        if _is_document(body):
            return "", body
    return "", b""


def _zip_kind(data: bytes) -> str:
    """识别 OOXML 压缩包类型：xlsx / docx / pptx / 其他空串。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except Exception:  # noqa: BLE001
        return ""
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    return ""


def _embedded_content(name: str, data: bytes,
                      depth: int) -> tuple[list[str], list[list[list[str]]]]:
    """把一个内嵌对象转成（说明行, 表格）；认不出的类型返回空，不报错。"""
    if len(data) > MAX_EMBED_BYTES:
        return ["内嵌对象 %s 超过 %dMB，已跳过"
                % (name, MAX_EMBED_BYTES // 1024 // 1024)], []

    kind = _zip_kind(data) if data[:4] == _ZIP_MAGIC else ""
    if kind == "xlsx":
        sheets, tables = _xlsx_bytes_tables(data)
        return ["内嵌表格：%s（工作表：%s）" % (name, "、".join(sheets[:MAX_TABLES]))], tables
    if kind == "docx" and depth < MAX_EMBED_DEPTH:
        from docx import Document

        sub_lines, sub_tables = _docx_body(Document(io.BytesIO(data)))
        return ["内嵌文档：%s" % name] + sub_lines, sub_tables[:MAX_TABLES]
    if kind:  # pptx 等其他 OOXML：不取内容也不报错
        return [], []

    if data[:8] == _OLE_MAGIC:
        import olefile

        try:
            ole = olefile.OleFileIO(io.BytesIO(data))
        except Exception:  # noqa: BLE001 - 结构损坏的 OLE 包
            return [], []
        try:
            real = {"/".join(part).lower(): "/".join(part) for part in ole.listdir()}
            # ① OLE Package：里面才是真正的原始文件
            if "package" in real:
                return _embedded_content(name, ole.openstream(real["package"]).read(), depth + 1)
            # ② 老式嵌入工作簿（xls 的 Workbook/Book 流）
            for wb_stream in ("workbook", "book"):
                if wb_stream in real:
                    sheets, tables = _xls_bytes_tables(ole.openstream(real[wb_stream]).read())
                    return ["内嵌工作簿：%s（工作表：%s）"
                            % (name, "、".join(sheets[:MAX_TABLES]))], tables
            # ③ Ole10Native：优先标准包，其次长度前缀裸数据（位图会在这里被丢弃）
            native = real.get("\x01olenative") or real.get("olenative")
            if native:
                _inner, payload = _ole10_payload(ole.openstream(native).read())
                if payload:
                    return _embedded_content(name, payload, depth + 1)
        finally:
            ole.close()
    return [], []


def _docx_embeddings(path) -> tuple[list[str], list[list[list[str]]]]:
    """扫描 ``word/embeddings/`` 下的内嵌对象；单个对象失败只记 debug 日志。"""
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist()
                     if n.startswith("word/embeddings/") and not n.endswith("/")]
            for name in names[:MAX_EMBED_OBJECTS]:
                short = name.rsplit("/", 1)[-1]
                try:
                    sub_lines, sub_tables = _embedded_content(short, z.read(name), 0)
                    lines.extend(sub_lines)
                    for table in sub_tables:
                        if len(tables) < MAX_TABLES:
                            tables.append(table)
                except Exception as exc:  # noqa: BLE001 - 单个内嵌对象失败不影响正文
                    log.debug("内嵌对象抽取失败 %s!%s: %s", path, name, exc)
    except Exception as exc:  # noqa: BLE001 - 压缩包异常交给上层
        log.debug("读取 docx 内嵌对象失败 %s: %s", path, exc)
    return lines, tables


def _from_pdf(path: Path) -> ExtractResult:
    """文本层用 pypdf；扫描页（无文本层）取内嵌图片走 OCR（M6-1）。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            return ExtractResult(error="PDF 已加密，无法抽取：%s" % exc)
    lines: list[str] = []
    for idx, page in enumerate(reader.pages):
        if idx >= MAX_PDF_PAGES:
            break
        text = page.extract_text() or ""
        if text.strip():
            lines.append(text)
            continue
        # 扫描 PDF 通常每页是一张内嵌位图；pypdf 可直接取出字节，无需额外渲染库。
        try:
            images = list(page.images)
        except Exception:  # noqa: BLE001
            images = []
        for image in images[:MAX_PDF_PAGES]:
            try:
                from .ocr import recognize_data

                recognized = recognize_data(image.data)
                if recognized.strip():
                    lines.append(recognized)
            except Exception as exc:  # noqa: BLE001
                log.debug("PDF 页图片 OCR 失败 %s: %s", path, exc)
    if not lines:
        return ExtractResult(error="PDF 无文本层且未取到可识别的内嵌图片")
    return ExtractResult(_clip("\n".join(lines)))


def _from_image(path: Path) -> ExtractResult:
    """图片扫描件：交给离线 OCR 引擎。"""
    from .ocr import get_engine

    engine = get_engine()
    if engine is None:
        return ExtractResult(error="无可用 OCR 引擎")
    try:
        text = engine.recognize(path)
    except Exception as exc:  # noqa: BLE001
        return ExtractResult(error="OCR 失败：%s" % exc)
    if not text.strip():
        return ExtractResult(error="OCR 未识别到文字")
    return ExtractResult(_clip(text))


def _from_text(path: Path) -> ExtractResult:
    from charset_normalizer import from_bytes

    raw = path.read_bytes()[: MAX_TEXT_CHARS * 4]
    best = from_bytes(raw).best()
    text = str(best) if best is not None else raw.decode("utf-8", errors="replace")
    return ExtractResult(_clip(text))


def _wps_convert(src: Path, kind: str, dest: Path) -> str:
    """用 WPS COM 把旧格式另存为 OOXML。返回空串表示成功，否则返回错误摘要。

    调用方必须持有 ``_COM_LOCK``。处于 ``wps_session`` 中时复用该线程的 WPS 实例，
    由 session 统一 Quit；否则自行 CoInitialize / Quit / CoUninitialize。
    """
    import pythoncom
    import win32com.client

    in_session = getattr(_local, "depth", 0) > 0
    if not in_session:
        pythoncom.CoInitialize()
    try:
        last = ""
        for progid in _WPS_PROGIDS[kind]:
            app = obj = None
            owned = False
            try:
                apps = getattr(_local, "apps", None) if in_session else None
                if apps is not None and progid in apps:
                    app = apps[progid]
                else:
                    app = win32com.client.DispatchEx(progid)
                    owned = True
                    if apps is not None:
                        apps[progid] = app
                try:
                    app.Visible = False
                except Exception:  # noqa: BLE001
                    pass
                try:
                    app.DisplayAlerts = 0
                except Exception:  # noqa: BLE001
                    pass

                if kind == "word":
                    obj = app.Documents.Open(
                        str(src), ReadOnly=True, ConfirmConversions=False,
                        AddToRecentFiles=False, Visible=False,
                    )
                else:
                    obj = app.Presentations.Open(str(src), ReadOnly=True, WithWindow=False)

                fmt = _WPS_FORMATS[kind]
                for method in ("SaveAs2", "SaveAs"):
                    fn = getattr(obj, method, None)
                    if fn is None:
                        continue
                    for call in (
                        lambda: fn(str(dest), FileFormat=fmt),
                        lambda: fn(str(dest), fmt),
                    ):
                        try:
                            call()
                            last = ""
                            break
                        except Exception as exc:  # noqa: BLE001
                            last = "%s.%s: %s" % (progid, method, exc)
                    if not last:
                        break
                if last:
                    raise RuntimeError(last)

                # WPS 存在「报成功但按原格式保存」的行为，必须校验 OOXML 的 ZIP 头。
                if not dest.exists() or dest.read_bytes()[:4] != b"PK\x03\x04":
                    raise RuntimeError("WPS 另存产物不是 OOXML（缺少 PK 头），FileFormat 可能被忽略")
                return ""
            except Exception as exc:  # noqa: BLE001
                last = "%s: %s: %s" % (progid, type(exc).__name__, exc)
                log.debug("WPS COM 转换失败 %s", last)
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
            finally:
                if obj is not None:
                    try:
                        obj.Close(0) if kind == "word" else obj.Close()
                    except Exception:  # noqa: BLE001
                        pass
                if app is not None and owned and not in_session:
                    try:
                        app.Quit()
                    except Exception:  # noqa: BLE001
                        pass
        return last or "WPS COM 不可用"
    finally:
        if not in_session:
            pythoncom.CoUninitialize()


def _via_wps(src: Path, kind: str) -> ExtractResult:
    # tempfile 默认落在 %TEMP%（本机为纯 ASCII 路径），避免中文路径下 COM 另存的编码坑。
    with tempfile.TemporaryDirectory(prefix="onlyone_wps_") as td:
        dest = Path(td) / ("converted" + _WPS_DEST_EXT[kind])
        with _COM_LOCK:
            err = _wps_convert(src, kind, dest)
        if err:
            return ExtractResult(error=err)
        result = extract(dest)
        if result.error:
            return ExtractResult(error="转换后抽取失败：%s" % result.error)
        return result


def extract(path: str | Path, *, with_images: bool = False) -> ExtractResult:
    """抽取单个文件；永不抛异常。未知后缀返回空正文（由扫描器仅索引文件名）。

    ``with_images=True`` 仅供只读预览：会在正文与表格单元格的图片位置插入
    ``[图 N]`` 标记，N 对应 ``images.list_images()`` 的序号加一。
    """
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if not p.is_file():
            return ExtractResult(error="文件不存在：%s" % p)
        if ext in TEXT_EXTS:
            return _from_text(p)
        if ext in IMAGE_EXTS:
            return _from_image(p)
        if ext == ".docx":
            return _from_docx(p, with_images)
        if ext == ".xlsx":
            return _from_xlsx(p)
        if ext == ".pptx":
            return _from_pptx(p)
        if ext == ".xls":
            return _from_xls(p)
        if ext == ".pdf":
            return _from_pdf(p)
        if ext in WPS_EXTS:
            return _via_wps(p, WPS_EXTS[ext])
        return ExtractResult()  # 未知格式：只索引文件名，不视为失败
    except Exception as exc:  # noqa: BLE001 - 单个文件失败不得中断扫描
        log.debug("抽取失败 %s: %s", p, exc)
        return ExtractResult(error="%s: %s" % (type(exc).__name__, exc))
