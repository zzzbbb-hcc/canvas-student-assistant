from io import BytesIO
from zipfile import ZipFile

from app.services.file_preview import extract_file_text, preview_kind


def zipped(files):
    target = BytesIO()
    with ZipFile(target, "w") as archive:
        for name, value in files.items(): archive.writestr(name, value)
    return target.getvalue()


def test_preview_kind_recognizes_courseware_formats():
    assert preview_kind({"content-type": "application/pdf"}) == "pdf"
    assert preview_kind({"display_name": "slides.pptx"}) == "pptx"
    assert preview_kind({"display_name": "worksheet.docx"}) == "docx"


def test_extracts_docx_text_without_executing_content():
    data = zipped({"word/document.xml": '<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Course notes</w:t></w:r></w:p></w:body></w:document>'})
    assert extract_file_text("docx", data) == "Course notes"


def test_extracts_pptx_text_by_slide():
    slide = '<p:sld xmlns:p="p" xmlns:a="a"><p:cSld><a:t>Lecture title</a:t></p:cSld></p:sld>'
    data = zipped({"ppt/slides/slide1.xml": slide})
    assert "Slide 1" in extract_file_text("pptx", data)
    assert "Lecture title" in extract_file_text("pptx", data)
