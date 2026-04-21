import argparse
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import pandas as pd
import pytesseract
from pdf2image import convert_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError
from pymongo import MongoClient, UpdateOne


# ==============================
# CONFIG
# ==============================
DEFAULT_PDF_NAME = "voter_list.pdf"
RAW_TEXT_PATH = "raw_output.txt"
EXCEL_OUTPUT_PATH = "voters.xlsx"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = "voter_db"
MONGO_COLLECTION = "voters"

OCR_LANG = "mar+eng"
OCR_CONFIG = "--oem 1 --psm 6"
DEFAULT_DPI = 220


def resolve_pdf_path(cli_pdf: Optional[str]) -> str:
    if cli_pdf:
        p = Path(cli_pdf)
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"PDF file not found: {cli_pdf}")

    env_pdf = os.getenv("PDF_PATH")
    if env_pdf and Path(env_pdf).exists():
        return env_pdf

    default = Path(DEFAULT_PDF_NAME)
    if default.exists():
        return str(default)

    pdfs = sorted(Path(".").glob("*.pdf"))
    if len(pdfs) == 1:
        return str(pdfs[0])
    if len(pdfs) > 1:
        names = ", ".join(p.name for p in pdfs[:5])
        raise FileNotFoundError(
            "Multiple PDF files found. Pass one explicitly, for example:\n"
            "python pdfToMongo.py --pdf your_file.pdf\n"
            f"Detected: {names}"
        )
    raise FileNotFoundError(
        "No PDF file found. Put a PDF in this folder or pass --pdf path_to_file.pdf"
    )


def resolve_poppler_path() -> Optional[str]:
    # If pdfinfo is already available in PATH, pdf2image works without poppler_path.
    if shutil.which("pdfinfo"):
        return None

    env_poppler = os.getenv("POPPLER_PATH")
    if env_poppler and Path(env_poppler).exists():
        return env_poppler

    candidates = [
        r"C:\poppler\Library\bin",
        r"C:\Program Files\poppler\Library\bin",
        r"C:\Program Files (x86)\poppler\Library\bin",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def configure_tesseract() -> None:
    if shutil.which("tesseract"):
        return

    env_tesseract = os.getenv("TESSERACT_CMD")
    if env_tesseract and Path(env_tesseract).exists():
        pytesseract.pytesseract.tesseract_cmd = env_tesseract
        return

    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            pytesseract.pytesseract.tesseract_cmd = path
            return


def configure_tessdata_prefix() -> None:
    if os.getenv("TESSDATA_PREFIX"):
        return
    user_tessdata = Path.home() / "tessdata"
    if user_tessdata.exists():
        os.environ["TESSDATA_PREFIX"] = str(user_tessdata)


def validate_tesseract(required_langs: str) -> None:
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        raise RuntimeError(
            "Tesseract OCR is not installed or not found.\n"
            "Install Tesseract for Windows, then set path if needed:\n"
            "$env:TESSERACT_CMD='C:\\Program Files\\Tesseract-OCR\\tesseract.exe'\n"
            "Download: https://github.com/UB-Mannheim/tesseract/wiki"
        ) from exc

    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception:
        # If language detection fails, do not block run here.
        return

    missing = [lang for lang in required_langs.split("+") if lang and lang not in available]
    if missing:
        missing_txt = ", ".join(missing)
        raise RuntimeError(
            f"Tesseract language data missing: {missing_txt}\n"
            "Install missing .traineddata files in your Tesseract tessdata folder.\n"
            "For Marathi, ensure mar.traineddata exists."
        )


def resolve_ocr_lang(preferred_langs: str) -> str:
    available = set(pytesseract.get_languages(config=""))
    preferred = [lang for lang in preferred_langs.split("+") if lang]

    if all(lang in available for lang in preferred):
        return preferred_langs
    if "eng" in available:
        print(
            f"Warning: preferred OCR languages '{preferred_langs}' not fully available. "
            "Falling back to 'eng'."
        )
        return "eng"
    if available:
        selected = sorted(available)[0]
        print(f"Warning: falling back to available OCR language '{selected}'.")
        return selected
    raise RuntimeError("No Tesseract languages available.")


# ==============================
# STEP 1: Convert PDF to Images
# ==============================
def pdf_to_images(pdf_path: str, dpi: int = DEFAULT_DPI, max_pages: Optional[int] = None):
    poppler_path = resolve_poppler_path()
    first_page = 1
    last_page = max_pages if max_pages and max_pages > 0 else None
    try:
        return convert_from_path(
            pdf_path,
            dpi=dpi,
            poppler_path=poppler_path,
            first_page=first_page,
            last_page=last_page,
        )
    except PDFInfoNotInstalledError as exc:
        # Fallback: use pypdfium2 (no external poppler install required).
        try:
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(pdf_path)
            images = []
            scale = dpi / 72
            page_count = len(pdf) if not max_pages else min(len(pdf), max_pages)
            for i in range(page_count):
                page = pdf[i]
                bitmap = page.render(scale=scale)
                images.append(bitmap.to_pil())
            return images
        except ImportError as imp_exc:
            raise RuntimeError(
                "Poppler is not configured and fallback renderer is not installed.\n"
                "Choose one option:\n"
                "1) Install Poppler and set POPPLER_PATH, or\n"
                "2) Install pypdfium2:\n"
                "   python -m pip install pypdfium2"
            ) from imp_exc
        except Exception as render_exc:
            raise RuntimeError(
                "Failed to render PDF via both pdf2image (Poppler) and pypdfium2 fallback.\n"
                "If you use Poppler, set:\n"
                "$env:POPPLER_PATH='C:\\poppler\\Library\\bin'"
            ) from render_exc


# ==============================
# STEP 2: Image Preprocessing
# ==============================
def preprocess_image(pil_image):
    img = np.array(pil_image)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        2,
    )
    return thresh


# ==============================
# STEP 3: OCR
# ==============================
def extract_text(images, ocr_lang: str) -> str:
    full_text = []
    for i, img in enumerate(images, start=1):
        processed = preprocess_image(img)
        text = pytesseract.image_to_string(processed, lang=ocr_lang, config=OCR_CONFIG)
        print(f"Processed page {i}")
        full_text.append(text)
    return "\n".join(full_text)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_age_gender(block: str) -> Tuple[Optional[int], Optional[str]]:
    mar_age = "\u0935\u092f"  # वय
    mar_male = "\u092a\u0941\u0930\u0941\u0937"  # पुरुष
    mar_female = "\u092e\u0939\u093f\u0932\u093e"  # महिला

    age_match = re.search(rf"(?:{mar_age}|Age)\s*[:\-]?\s*(\d{{1,3}})", block, flags=re.IGNORECASE)
    gender_match = re.search(
        rf"({mar_male}|{mar_female}|Male|Female)", block, flags=re.IGNORECASE
    )

    age = int(age_match.group(1)) if age_match else None
    gender = gender_match.group(1) if gender_match else None

    if gender:
        g = gender.lower()
        if g == "male":
            gender = mar_male
        elif g == "female":
            gender = mar_female
    return age, gender


def extract_name_fields(block: str) -> Tuple[Optional[str], Optional[str]]:
    mar_name = "\u0928\u093e\u0935"  # नाव
    mar_father = "\u0935\u0921\u093f\u0932\u093e\u0902\u091a\u0947 \u0928\u093e\u0935"  # वडिलांचे नाव
    mar_husband = "\u092a\u0924\u0940\u091a\u0947 \u0928\u093e\u0935"  # पतीचे नाव
    mar_mother = "\u0906\u0908\u091a\u0947 \u0928\u093e\u0935"  # आईचे नाव

    name_patterns = [
        rf"(?:{mar_name}|Name)\s*[:\-]?\s*([^\n]+)",
    ]
    relative_patterns = [
        rf"(?:{mar_father}|{mar_husband}|{mar_mother}|Father(?:'s)? Name|Husband(?:'s)? Name|Mother(?:'s)? Name)\s*[:\-]?\s*([^\n]+)",
    ]

    name = None
    relative_name = None

    for pattern in name_patterns:
        m = re.search(pattern, block, flags=re.IGNORECASE)
        if m:
            name = clean_text(m.group(1))
            break

    for pattern in relative_patterns:
        m = re.search(pattern, block, flags=re.IGNORECASE)
        if m:
            relative_name = clean_text(m.group(1))
            break

    return name, relative_name


# ==============================
# STEP 4: Parse Structured Data
# ==============================
def parse_voter_data(text: str, source_file: str) -> List[Dict]:
    records: List[Dict] = []
    blocks = re.findall(r"(?ms)^\s*(\d+)\s+(.+?)(?=^\s*\d+\s+|\Z)", text)

    for serial_no, block in blocks:
        block = block.strip()
        if len(block) < 5:
            continue

        name, relative_name = extract_name_fields(block)
        age, gender = extract_age_gender(block)

        if not name:
            lines = [clean_text(x) for x in block.splitlines() if clean_text(x)]
            if lines:
                name = lines[0]
            if len(lines) > 1:
                relative_name = relative_name or lines[1]

        record = {
            "serial_no": int(serial_no),
            "name": name,
            "relative_name": relative_name,
            "age": age,
            "gender": gender,
            "source_file": source_file,
        }

        if record["name"]:
            records.append(record)

    return records


# ==============================
# STEP 5: Save to Excel
# ==============================
def save_to_excel(data: List[Dict], output_path: str = EXCEL_OUTPUT_PATH):
    if not data:
        print("No data to export to Excel")
        return

    df = pd.DataFrame(data)
    cols = ["serial_no", "name", "relative_name", "age", "gender", "source_file"]
    df = df.reindex(columns=cols)
    df.to_excel(output_path, index=False)
    print(f"Excel written: {output_path} ({len(df)} rows)")


# ==============================
# STEP 6: Save to MongoDB
# ==============================
def save_to_mongodb(data: List[Dict]):
    if not data:
        print("No data to insert in MongoDB")
        return

    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    collection = db[MONGO_COLLECTION]

    collection.create_index([("source_file", 1), ("serial_no", 1)], unique=True)

    ops = [
        UpdateOne(
            {"source_file": item["source_file"], "serial_no": item["serial_no"]},
            {"$set": item},
            upsert=True,
        )
        for item in data
    ]
    result = collection.bulk_write(ops, ordered=False)
    print(
        "MongoDB upsert complete: "
        f"matched={result.matched_count}, modified={result.modified_count}, "
        f"upserted={len(result.upserted_ids)}"
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Convert voter PDF to MongoDB + Excel")
    parser.add_argument("--pdf", help="PDF filename/path", default=None)
    parser.add_argument("--excel", help="Output Excel path", default=EXCEL_OUTPUT_PATH)
    parser.add_argument("--raw", help="Raw OCR text output path", default=RAW_TEXT_PATH)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="PDF render DPI (faster at lower values)")
    parser.add_argument("--max-pages", type=int, default=None, help="Process only first N pages for quick test")
    return parser.parse_args()


# ==============================
# MAIN PIPELINE
# ==============================
def main():
    args = parse_args()

    configure_tesseract()
    configure_tessdata_prefix()
    validate_tesseract(OCR_LANG)
    ocr_lang = resolve_ocr_lang(OCR_LANG)
    pdf_path = resolve_pdf_path(args.pdf)
    source_file = Path(pdf_path).name
    print(f"Using PDF: {source_file}")
    print(f"OCR language: {ocr_lang}")

    print("Step 1: Converting PDF to images...")
    images = pdf_to_images(pdf_path, dpi=args.dpi, max_pages=args.max_pages)

    print("Step 2: Extracting text via OCR...")
    text = extract_text(images, ocr_lang=ocr_lang)

    with open(args.raw, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Raw OCR text saved: {args.raw}")

    print("Step 3: Parsing structured data...")
    data = parse_voter_data(text, source_file=source_file)
    print(f"Parsed records: {len(data)}")

    print("Step 4: Saving to Excel...")
    save_to_excel(data, args.excel)

    print("Step 5: Saving to MongoDB...")
    save_to_mongodb(data)

    print("Done")


if __name__ == "__main__":
    main()
