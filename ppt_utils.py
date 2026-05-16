from pptx import Presentation
from pypdf import PdfReader

def extract_text_from_ppt(file_path):
    prs = Presentation(file_path)
    text = ""

    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                text += shape.text + "\n"

    return text

def extract_text_from_pdf(file_path):
    reader = PdfReader(file_path)
    pages = []

    for page in reader.pages:
        pages.append(page.extract_text() or "")

    return "\n".join(pages)

def extract_text_from_file(file_path):
    lower_path = file_path.lower()

    if lower_path.endswith((".ppt", ".pptx")):
        return extract_text_from_ppt(file_path)
    if lower_path.endswith(".pdf"):
        return extract_text_from_pdf(file_path)

    raise ValueError("Unsupported file type. Please upload a PPT, PPTX, or PDF file.")
