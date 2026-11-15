from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


def preview_kind(metadata: dict) -> str:
    mime = (metadata.get("content-type") or "").lower()
    name = (metadata.get("display_name") or metadata.get("filename") or "").lower()
    if mime == "application/pdf" or name.endswith(".pdf"): return "pdf"
    if mime.startswith("image/"): return "image"
    if name.endswith(".docx"): return "docx"
    if name.endswith(".pptx"): return "pptx"
    if name.endswith(".xlsx"): return "xlsx"
    if mime.startswith("text/") or any(name.endswith(ext) for ext in (".txt", ".md", ".csv", ".json", ".py", ".java", ".js", ".html", ".css", ".sql", ".r")): return "text"
    return "unsupported"


def extract_file_text(kind: str, content: bytes) -> str:
    if kind == "text":
        return content[:2_000_000].decode("utf-8", errors="replace")
    if kind not in {"docx", "pptx", "xlsx"}: return ""
    try:
        with ZipFile(BytesIO(content)) as archive:
            if kind == "docx": return _docx_text(archive)
            if kind == "pptx": return _pptx_text(archive)
            return _xlsx_text(archive)
    except (BadZipFile, KeyError, ElementTree.ParseError, OSError):
        return "This Office file could not be read safely."


def _xml_text(data: bytes, text_suffix: str) -> list[str]:
    root = ElementTree.fromstring(data)
    return [node.text or "" for node in root.iter() if node.tag.endswith(text_suffix) and node.text]


def _docx_text(archive: ZipFile) -> str:
    root = ElementTree.fromstring(archive.read("word/document.xml")); paragraphs = []
    for paragraph in (node for node in root.iter() if node.tag.endswith("}p")):
        text = "".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t"))
        if text.strip(): paragraphs.append(text)
    return "\n\n".join(paragraphs) or "No readable text was found in this document."


def _pptx_text(archive: ZipFile) -> str:
    names = [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
    names.sort(key=lambda name: int(PurePosixPath(name).stem.removeprefix("slide")))
    slides = []
    for number, name in enumerate(names, 1):
        lines = _xml_text(archive.read(name), "}t")
        slides.append(f"Slide {number}\n" + ("\n".join(lines) if lines else "No readable text"))
    return "\n\n".join(slides) or "No readable slides were found."


def _xlsx_text(archive: ZipFile) -> str:
    if "xl/sharedStrings.xml" not in archive.namelist(): return "No readable shared text was found in this workbook."
    values = _xml_text(archive.read("xl/sharedStrings.xml"), "}t")
    return "\n".join(values) or "No readable text was found in this workbook."
