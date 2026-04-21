import argparse
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


def preprocess_image(pil_image):
    img = np.array(pil_image)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2
    )
    return thresh


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def resolve_poppler_path() -> Optional[str]:
    if shutil.which("pdfinfo"):
        return None
    return None


def pdf_to_images(pdf_path: Path, dpi: int, max_pages: Optional[int]):
    poppler_path = resolve_poppler_path()
    first_page = 1
    last_page = max_pages if max_pages and max_pages > 0 else None
    try:
        return convert_from_path(
            str(pdf_path),
            dpi=dpi,
            poppler_path=poppler_path,
            first_page=first_page,
            last_page=last_page,
        )
    except PDFInfoNotInstalledError:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(pdf_path))
        scale = dpi / 72
        page_count = len(pdf) if not max_pages else min(len(pdf), max_pages)
        images = []
        for i in range(page_count):
            page = pdf[i]
            images.append(page.render(scale=scale).to_pil())
        return images


def extract_text(images, ocr_lang: str, ocr_config: str) -> str:
    parts = []
    for idx, img in enumerate(images, start=1):
        processed = preprocess_image(img)
        txt = pytesseract.image_to_string(processed, lang=ocr_lang, config=ocr_config)
        print(f"      OCR page {idx}/{len(images)}")
        parts.append(txt)
    return "\n".join(parts)


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

    name_patterns = [rf"(?:{mar_name}|Name)\s*[:\-]?\s*([^\n]+)"]
    relative_patterns = [
        rf"(?:{mar_father}|{mar_husband}|{mar_mother}|Father(?:'s)? Name|Husband(?:'s)? Name|Mother(?:'s)? Name)\s*[:\-]?\s*([^\n]+)"
    ]

    name = None
    relative_name = None

    for p in name_patterns:
        m = re.search(p, block, flags=re.IGNORECASE)
        if m:
            name = clean_text(m.group(1))
            break

    for p in relative_patterns:
        m = re.search(p, block, flags=re.IGNORECASE)
        if m:
            relative_name = clean_text(m.group(1))
            break

    return name, relative_name


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

        rec = {
            "serial_no": int(serial_no),
            "name": name,
            "relative_name": relative_name,
            "age": age,
            "gender": gender,
            "source_file": source_file,
        }
        if rec["name"]:
            records.append(rec)
    return records


def save_excel(records: List[Dict], excel_path: Path) -> None:
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["serial_no", "name", "relative_name", "age", "gender", "source_file"]
    df = pd.DataFrame(records).reindex(columns=cols)
    df.to_excel(excel_path, index=False)
    print(f"Combined Excel written: {excel_path} ({len(df)} rows)")


def save_mongo(records: List[Dict], mongo_uri: str, mongo_db: str, mongo_collection: str) -> None:
    if not mongo_uri:
        print("MongoDB skipped (no --mongo-uri provided).")
        return
    if not records:
        print("MongoDB skipped (no records).")
        return

    client = MongoClient(mongo_uri)
    collection = client[mongo_db][mongo_collection]
    collection.create_index([("source_file", 1), ("serial_no", 1)], unique=True)
    ops = [
        UpdateOne(
            {"source_file": r["source_file"], "serial_no": r["serial_no"]},
            {"$set": r},
            upsert=True,
        )
        for r in records
    ]
    result = collection.bulk_write(ops, ordered=False)
    print(
        "MongoDB upsert complete: "
        f"matched={result.matched_count}, modified={result.modified_count}, "
        f"upserted={len(result.upserted_ids)}"
    )


def resolve_ocr_lang(preferred: str) -> str:
    langs = set(pytesseract.get_languages(config=""))
    req = [x for x in preferred.split("+") if x]
    if all(x in langs for x in req):
        return preferred
    if "eng" in langs:
        print(f"Warning: '{preferred}' not available. Falling back to 'eng'.")
        return "eng"
    if langs:
        fallback = sorted(langs)[0]
        print(f"Warning: Falling back to '{fallback}'.")
        return fallback
    raise RuntimeError("No OCR languages found in Tesseract.")


def parse_args():
    p = argparse.ArgumentParser(description="Batch convert PDFs to Excel and optional MongoDB")
    p.add_argument("--input-dir", default="pdfs", help="Folder containing PDF files")
    p.add_argument("--excel", default="output/all_voters.xlsx", help="Combined Excel output path")
    p.add_argument("--raw-dir", default="output/raw_text", help="Raw OCR text output folder")
    p.add_argument("--lang", default="mar+eng", help="OCR language(s), e.g. mar+eng")
    p.add_argument("--dpi", type=int, default=180, help="Render DPI (higher is slower)")
    p.add_argument("--max-pages", type=int, default=None, help="Only process first N pages per PDF")
    p.add_argument("--mongo-uri", default="", help="Mongo URI. Leave empty to skip Mongo.")
    p.add_argument("--mongo-db", default="voter_db", help="Mongo database name")
    p.add_argument("--mongo-collection", default="voters", help="Mongo collection name")
    return p.parse_args()


def main():
    args = parse_args()
    input_dir = Path(args.input_dir)
    raw_dir = Path(args.raw_dir)
    excel_path = Path(args.excel)

    if not input_dir.exists():
        raise FileNotFoundError(f"Input folder not found: {input_dir}")

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in: {input_dir}")

    raw_dir.mkdir(parents=True, exist_ok=True)
    ocr_lang = resolve_ocr_lang(args.lang)
    ocr_config = "--oem 1 --psm 6"

    all_records: List[Dict] = []
    print(f"Found {len(pdf_files)} PDF files.")

    for idx, pdf in enumerate(pdf_files, start=1):
        print(f"[{idx}/{len(pdf_files)}] Processing: {pdf.name}")
        images = pdf_to_images(pdf, dpi=args.dpi, max_pages=args.max_pages)
        text = extract_text(images, ocr_lang=ocr_lang, ocr_config=ocr_config)

        raw_file = raw_dir / f"{pdf.stem}.txt"
        raw_file.write_text(text, encoding="utf-8")

        records = parse_voter_data(text, source_file=pdf.name)
        print(f"      Parsed records: {len(records)}")
        all_records.extend(records)

    save_excel(all_records, excel_path)
    save_mongo(
        all_records,
        mongo_uri=args.mongo_uri,
        mongo_db=args.mongo_db,
        mongo_collection=args.mongo_collection,
    )
    print("Done.")


if __name__ == "__main__":
    main()
