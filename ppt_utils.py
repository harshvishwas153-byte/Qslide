from pptx import Presentation
from pypdf import PdfReader

def extract_text_from_pptx(file_path):
    try:
        prs = Presentation(file_path)
    except Exception as exc:
        raise ValueError(
            "This PowerPoint file could not be read. Please upload a valid .pptx file, "
            "or export the presentation as PDF and upload the PDF."
        ) from exc

    text = ""

    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                text += shape.text + "\n"

    return text

def extract_text_from_pdf(file_path):
    try:
        reader = PdfReader(file_path)
    except Exception as exc:
        raise ValueError(
            "This PDF file could not be read. Please upload a valid text-based PDF."
        ) from exc

    pages = []

    for page in reader.pages:
        pages.append(page.extract_text() or "")

    return "\n".join(pages)

def extract_text_from_file(file_path):
    lower_path = file_path.lower()

    if lower_path.endswith(".pptx"):
        return extract_text_from_pptx(file_path)
    if lower_path.endswith(".ppt"):
        raise ValueError(
            "Old .ppt files are not supported. Please save the presentation as .pptx "
            "or export it as PDF, then upload again."
        )
    if lower_path.endswith(".pdf"):
        return extract_text_from_pdf(file_path)

    raise ValueError("Unsupported file type. Please upload a PPTX or PDF file.")
