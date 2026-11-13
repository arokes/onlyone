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

import logging
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("onlyone")

MAX_TEXT_CHARS = 100_000
MAX_TABLE_ROWS = 2_000
MAX_TABLE_COLS = 40
MAX_TABLES = 20
MAX_PDF_PAGES = 500

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


def _from_docx(path: Path) -> ExtractResult:
    from docx import Document

    doc = Document(str(path))
    lines = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    tables: list[list[list[str]]] = []
    for table in doc.tables[:MAX_TABLES]:
        rows = [
            [cell.text.strip().replace("\n", " ") for cell in row.cells[:MAX_TABLE_COLS]]
            for row in table.rows[:MAX_TABLE_ROWS]
        ]
        if rows:
            tables.append(rows)
    return ExtractResult(_clip("\n".join(lines)), tables)


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
        rows = [
            [str(sheet.cell_value(r, c)) for c in range(min(sheet.ncols, MAX_TABLE_COLS))]
            for r in range(min(sheet.nrows, MAX_TABLE_ROWS))
        ]
        if rows:
            tables.append(rows)
        if len(tables) >= MAX_TABLES:
            break
    return ExtractResult(_clip("\n".join(lines)), tables)


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


def extract(path: str | Path) -> ExtractResult:
    """抽取单个文件；永不抛异常。未知后缀返回空正文（由扫描器仅索引文件名）。"""
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
            return _from_docx(p)
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
