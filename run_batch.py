import argparse
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import cv2
except ImportError:
    cv2 = None

import numpy as np
import pandas as pd

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from pdf2image import convert_from_path
    from pdf2image.exceptions import PDFInfoNotInstalledError
except ImportError:
    convert_from_path = None
    PDFInfoNotInstalledError = Exception

try:
    from pymongo import MongoClient, UpdateOne
except ImportError:
    MongoClient = None
    UpdateOne = None


MR_MALE = "\u092a\u0941\u0930\u0941\u0937"  # पुरुष
MR_FEMALE = "\u092e\u0939\u093f\u0932\u093e"  # महिला
REL_CODE_FATHER = "\u0935"  # व
REL_CODE_HUSBAND = "\u092a"  # प

SERIAL_LINE_RE = re.compile(r"^([0-9\u0966-\u096f]{1,4})\s+(.+)$")
DEFAULT_PSM = 6


DEV_VOWELS = {
    "\u0905": "a", "\u0906": "aa", "\u0907": "i", "\u0908": "ii", "\u0909": "u", "\u090a": "uu",
    "\u090b": "ri", "\u090f": "e", "\u0910": "ai", "\u0913": "o", "\u0914": "au",
}
DEV_MATRAS = {
    "\u093e": "aa", "\u093f": "i", "\u0940": "ii", "\u0941": "u", "\u0942": "uu",
    "\u0943": "ri", "\u0947": "e", "\u0948": "ai", "\u094b": "o", "\u094c": "au",
}
DEV_CONS = {
    "\u0915": "k", "\u0916": "kh", "\u0917": "g", "\u0918": "gh", "\u0919": "ng",
    "\u091a": "ch", "\u091b": "chh", "\u091c": "j", "\u091d": "jh", "\u091e": "ny",
    "\u091f": "t", "\u0920": "th", "\u0921": "d", "\u0922": "dh", "\u0923": "n",
    "\u0924": "t", "\u0925": "th", "\u0926": "d", "\u0927": "dh", "\u0928": "n",
    "\u092a": "p", "\u092b": "ph", "\u092c": "b", "\u092d": "bh", "\u092e": "m",
    "\u092f": "y", "\u0930": "r", "\u0932": "l", "\u0935": "v", "\u0936": "sh",
    "\u0937": "sh", "\u0938": "s", "\u0939": "h", "\u0933": "l",
}
DEV_SIGNS = {"\u0902": "n", "\u0903": "h", "\u0901": "n", "\u094d": ""}
DEV_DIGITS = {
    "\u0966": "0", "\u0967": "1", "\u0968": "2", "\u0969": "3", "\u096a": "4",
    "\u096b": "5", "\u096c": "6", "\u096d": "7", "\u096e": "8", "\u096f": "9",
}


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_token(token: str) -> str:
    return token.strip("`'\"|[](){}.,:;*_")


def contains_devanagari(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text))


def devanagari_digits_to_ascii(text: str) -> str:
    out = []
    for ch in text:
        out.append(DEV_DIGITS.get(ch, ch))
    return "".join(out)


def ascii_digits_only(text: str) -> str:
    return re.sub(r"\D", "", devanagari_digits_to_ascii(text))


def parse_age_from_token(token: str) -> Optional[int]:
    d = ascii_digits_only(token)
    if not d:
        return None

    # Ignore long numeric chunks; these are usually voter IDs.
    if len(d) >= 5:
        return None

    n = int(d)
    if 18 <= n <= 110:
        return n

    # OCR often appends one extra digit (e.g. 271 -> 27, 346 -> 34).
    if len(d) >= 3:
        first_two = int(d[:2])
        if 18 <= first_two <= 110:
            return first_two
        last_two = int(d[-2:])
        if 18 <= last_two <= 110:
            return last_two
    return None


def normalize_house_no(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    t = normalize_token(token)
    if not t:
        return None
    one_like = {"i", "l", "|", "!", "it", "lt", "1t"}
    if t.lower() in one_like:
        return "1"
    return t


def normalize_gender(token: str) -> Optional[str]:
    t = normalize_token(token).lower()
    male_tokens = {"\u092a\u0941", "\u092a\u0943", "\u092a\u0941\u0930\u0941\u0937", "q", "qy", "aq", "pu"}
    female_tokens = {
        "\u0938\u094d\u0924\u094d\u0930\u0940",
        "\u0938\u094d\u0935\u0940",
        "\u0938\u094d\u0924\u094d\u0935\u0940",
        "et",
        "wt",
        "emt",
        "eh",
        "wh",
    }
    if t in male_tokens:
        return MR_MALE
    if t in female_tokens:
        return MR_FEMALE
    if "\u0938\u094d\u0924\u094d\u0930\u0940" in t or "\u0938\u094d\u0935\u0940" in t:
        return MR_FEMALE
    if t == "male":
        return MR_MALE
    if t == "female":
        return MR_FEMALE
    return None


def normalize_relation_code(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    t = normalize_token(token).lower()
    father_like = {"a", "aq", "ap", "q", "v", "va", "w", "wa", "vq", "av", "\u0935"}
    husband_like = {"p", "pa", "7", "\u092a"}
    if t in father_like:
        return REL_CODE_FATHER
    if t in husband_like:
        return REL_CODE_HUSBAND
    return None


def to_english_gender(gender: Optional[str]) -> Optional[str]:
    if gender == MR_MALE:
        return "Male"
    if gender == MR_FEMALE:
        return "Female"
    return None


def to_english_relation_short(rel_code: Optional[str]) -> Optional[str]:
    if rel_code == REL_CODE_FATHER:
        return "Fat"
    if rel_code == REL_CODE_HUSBAND:
        return "Fus"
    return None


def transliterate_devanagari(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    s = str(text)
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in DEV_DIGITS:
            out.append(DEV_DIGITS[ch])
            i += 1
            continue
        if ch in DEV_VOWELS:
            out.append(DEV_VOWELS[ch])
            i += 1
            continue
        if ch in DEV_CONS:
            base = DEV_CONS[ch]
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if nxt == "\u094d":
                out.append(base)
                i += 2
                continue
            if nxt in DEV_MATRAS:
                out.append(base + DEV_MATRAS[nxt])
                i += 2
                continue
            out.append(base + "a")
            i += 1
            continue
        if ch in DEV_MATRAS:
            out.append(DEV_MATRAS[ch])
            i += 1
            continue
        if ch in DEV_SIGNS:
            out.append(DEV_SIGNS[ch])
            i += 1
            continue
        out.append(ch)
        i += 1
    roman = clean_text("".join(out))
    roman = roman.replace("aa", "a").replace("ii", "i").replace("uu", "u")
    roman = re.sub(r"[^A-Za-z0-9/ -]+", " ", roman)
    roman = re.sub(r"\s+", " ", roman).strip()
    return " ".join(w.lower().capitalize() for w in roman.split())


def preprocess_image(pil_image, mode: str = "adaptive"):
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) is required. Install: pip install opencv-python")
    img = np.array(pil_image)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if mode == "gray":
        return gray
    if mode == "otsu":
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, out = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return out
    if mode == "adaptive":
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        return cv2.adaptiveThreshold(
            blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2
        )
    raise ValueError(f"Unknown preprocess mode: {mode}")


def build_ocr_config(psm: int) -> str:
    return f"--oem 1 --psm {psm} -c preserve_interword_spaces=1"


def get_ocr_variants(accuracy_mode: str) -> List[Tuple[str, str, int]]:
    if accuracy_mode == "fast":
        return [("adaptive-psm6", "adaptive", DEFAULT_PSM)]
    if accuracy_mode == "high":
        return [
            ("adaptive-psm6", "adaptive", DEFAULT_PSM),
            ("otsu-psm6", "otsu", 6),
            ("adaptive-psm4", "adaptive", 4),
            ("gray-psm6", "gray", DEFAULT_PSM),
        ]
    # balanced (default)
    return [
        ("adaptive-psm6", "adaptive", DEFAULT_PSM),
        ("otsu-psm6", "otsu", 6),
    ]


def configure_tesseract() -> None:
    if pytesseract is None:
        raise RuntimeError("pytesseract is required. Install: pip install pytesseract")
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
    candidates = [
        Path.home() / "tessdata",
        Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tessdata"),
    ]
    for path in candidates:
        if path.exists():
            os.environ["TESSDATA_PREFIX"] = str(path)
            return


def resolve_poppler_path() -> Optional[str]:
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


def render_pdf_with_pdfium(pdf_path: Path, dpi: int, max_pages: Optional[int]):
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "PDF rendering needs Poppler or pypdfium2. Install one:\n"
            "  python -m pip install pypdfium2\n"
            "or install Poppler and set POPPLER_PATH."
        ) from exc

    pdf = pdfium.PdfDocument(str(pdf_path))
    scale = dpi / 72
    page_count = len(pdf) if not max_pages else min(len(pdf), max_pages)
    images = []
    for i in range(page_count):
        page = pdf[i]
        images.append(page.render(scale=scale).to_pil())
    return images


def pdf_to_images(pdf_path: Path, dpi: int, max_pages: Optional[int]):
    if convert_from_path is None:
        return render_pdf_with_pdfium(pdf_path, dpi=dpi, max_pages=max_pages)
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
        return render_pdf_with_pdfium(pdf_path, dpi=dpi, max_pages=max_pages)


def score_ocr_text(text: str) -> int:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    serial_hits = sum(1 for ln in lines if SERIAL_LINE_RE.match(ln))
    devanagari_chars = len(re.findall(r"[\u0900-\u097F]", text))
    # Reuse parser as a quality signal: more valid records usually means better OCR.
    parsed_hits = len(parse_voter_data(text, source_file="_ocr_score_", constants={}))
    return (parsed_hits * 500) + (serial_hits * 25) + (devanagari_chars // 50)


def extract_text(images, ocr_lang: str, accuracy_mode: str) -> str:
    if pytesseract is None:
        raise RuntimeError("pytesseract is required. Install: pip install pytesseract")
    variants = get_ocr_variants(accuracy_mode)
    parts = []
    for idx, img in enumerate(images, start=1):
        best_text = ""
        best_variant = variants[0][0]
        best_score = -1
        for variant_name, preprocess_mode, psm in variants:
            processed = preprocess_image(img, mode=preprocess_mode)
            txt = pytesseract.image_to_string(
                processed,
                lang=ocr_lang,
                config=build_ocr_config(psm),
            )
            score = score_ocr_text(txt)
            if score > best_score:
                best_score = score
                best_text = txt
                best_variant = variant_name
        print(f"      OCR page {idx}/{len(images)} [{best_variant}]")
        parts.append(best_text)
    return "\n".join(parts)


def resolve_ocr_lang(preferred: str) -> str:
    if pytesseract is None:
        raise RuntimeError("pytesseract is required. Install: pip install pytesseract")
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
    raise RuntimeError("No OCR languages found.")


def _find_first_group(text: str, patterns: List[str]) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return clean_text(m.group(1))
    return None


def _find_first_two_groups(text: str, patterns: List[str]) -> Tuple[Optional[str], Optional[str]]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return clean_text(m.group(1)), clean_text(m.group(2))
    return None, None


def extract_constants(text: str, source_file: str) -> Dict[str, Optional[str]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = "\n".join(lines[:350])
    full = text

    const = {
        "lit_state_code": None,
        "lit_ac_no": None,
        "lit_ac_name": None,
        "lit_part_no": None,
        "lit_pc_no": None,
        "lit_pc_name": None,
        "lit_reservation": None,
        "lit_revision_year": None,
        "lit_qualifying_date": None,
        "lit_main_town": None,
        "lit_saja": None,
        "lit_mandal": None,
        "lit_tahsil": None,
        "lit_district": None,
        "lit_polling_station_no": None,
        "lit_polling_station_name": None,
        "lit_total_voters": None,
    }

    stem = Path(source_file).stem
    fm = re.search(r"(\d{2,3})_(\d{3})_(\d{3})$", stem)
    if fm:
        const["lit_state_code"] = fm.group(1)
        const["lit_ac_no"] = fm.group(2)
        const["lit_part_no"] = fm.group(3)

    ac_no, ac_name = _find_first_two_groups(
        full,
        [
            r"\((\d{1,4})\)\s*[-–]?\s*([^\n]+?)\s+विधानसभा",
            r"विधानसभा\s+मतदारसंघाचा\s+क्रमांक[, ]*नाव\s*[:：]\s*(\d{1,4})\s*[-–]?\s*([^\n]+)",
        ],
    )
    if ac_no:
        const["lit_ac_no"] = ac_no
    if ac_name:
        const["lit_ac_name"] = ac_name

    part_no = _find_first_group(
        head,
        [
            r"यादी\s*भाग\s*क्रमांक\s*[:：]?\s*([0-9]{1,4})",
            r"यादी\s*भाग\s*[:：]?\s*([0-9]{1,4})",
        ],
    )
    if part_no:
        const["lit_part_no"] = part_no

    pc_no, pc_name = _find_first_two_groups(
        head,
        [
            r"लोकसभा\s+मतदारसंघाचा\s+क्रमांक[, ]*नाव\s*[:：]\s*(\d{1,3})\s*[-–]?\s*([^\n]+)",
        ],
    )
    if pc_no:
        const["lit_pc_no"] = pc_no
    if pc_name:
        const["lit_pc_name"] = pc_name

    ps_no, ps_name = _find_first_two_groups(
        head,
        [
            r"मतदान\s*केंद्र\s*अनुक्रमांक\s*आणि\s*नाव\s*[:：]\s*([0-9]{1,4})\s+([^\n]+)",
        ],
    )
    if ps_no:
        const["lit_polling_station_no"] = ps_no
    if ps_name:
        const["lit_polling_station_name"] = ps_name

    const["lit_total_voters"] = _find_first_group(
        head,
        [
            r"यादी\s*भागातील\s*एकूण\s*संख्या\s*[:：]?\s*([0-9\u0966-\u096f]{3,5})",
        ],
    )
    if const["lit_total_voters"]:
        total_txt = devanagari_digits_to_ascii(const["lit_total_voters"])
        d = re.sub(r"\D", "", total_txt)
        if d.isdigit() and int(d) >= 100:
            const["lit_total_voters"] = d
        else:
            const["lit_total_voters"] = None

    const["lit_reservation"] = _find_first_group(
        head, [r"आरक्षण\s*स्थिती\s*[:：]\s*([^\n]+)"]
    )
    const["lit_revision_year"] = _find_first_group(
        head, [r"पुनरीक्षण\s*वर्ष\s*[:：]\s*([0-9]{4})"]
    )
    const["lit_qualifying_date"] = _find_first_group(
        head, [r"अर्हता\s*दिनांक\s*[:：]\s*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})"]
    )
    const["lit_main_town"] = _find_first_group(
        head, [r"मू[ळल]\s*गाव\s*शहर\s*[:：]\s*([^\n]+)"]
    )
    const["lit_saja"] = _find_first_group(
        head, [r"(?:सजा|राजा)\s*[:：]\s*([^\n]+)"]
    )
    const["lit_mandal"] = _find_first_group(
        head, [r"मंडळ\s*[:：]\s*([^\n]+)"]
    )
    const["lit_tahsil"] = _find_first_group(
        head, [r"तहसील\s*[:：]\s*([^\n]+)"]
    )
    const["lit_district"] = _find_first_group(
        head, [r"जिल्हा\s*[:：]\s*([^\n]+)"]
    )

    for k, v in list(const.items()):
        if v is None:
            continue
        vv = clean_text(v)
        vv = re.sub(r"[|]+$", "", vv).strip(" .:-")
        const[k] = vv if vv else None

    return const


def correct_serial_number(
    raw_serial: str,
    prev_serial: Optional[int],
    expected_total: Optional[int],
) -> Optional[int]:
    digits = ascii_digits_only(raw_serial)
    if not digits:
        return None

    limit = expected_total if expected_total else 999999
    serial = int(digits)

    if prev_serial is None:
        if 0 < serial <= limit:
            return serial
        return None

    if 0 < serial <= limit and serial > prev_serial and serial - prev_serial <= 50:
        return serial

    # Repair OCR serials that include one extra digit (e.g. 371 instead of 31).
    if len(digits) >= 3 and (serial <= prev_serial or serial - prev_serial > 50):
        candidates: List[Tuple[int, int]] = []
        for i in range(len(digits)):
            cand_raw = digits[:i] + digits[i + 1 :]
            if not cand_raw:
                continue
            cand = int(cand_raw)
            if cand <= 0 or cand > limit:
                continue
            gap = cand - prev_serial
            if 0 < gap <= 5:
                candidates.append((gap, cand))
        if candidates:
            candidates.sort()
            return candidates[0][1]

    if serial <= 0 or serial > limit:
        return None
    return serial


def merge_record_lines(lines: List[str], max_extra_lines: int = 2) -> List[str]:
    merged: List[str] = []
    i = 0
    while i < len(lines):
        line = clean_text(lines[i])
        m = SERIAL_LINE_RE.match(line)
        if not m:
            i += 1
            continue

        combined = line
        j = i + 1
        extras = 0
        while j < len(lines) and extras < max_extra_lines:
            nxt = clean_text(lines[j])
            if not nxt:
                j += 1
                continue
            if SERIAL_LINE_RE.match(nxt):
                break
            combined = f"{combined} {nxt}"
            extras += 1
            j += 1

        merged.append(clean_text(combined))
        i = j
    return merged


def parse_age_id_gender(tokens: List[str]) -> Tuple[Optional[int], Optional[int], Optional[str], Optional[int]]:
    age = None
    age_idx = None
    gender = None
    gender_idx = None

    for i in range(len(tokens) - 1, -1, -1):
        parsed_age = parse_age_from_token(tokens[i])
        if parsed_age is not None:
            age = parsed_age
            age_idx = i
            break

    if age_idx is None:
        return None, None, None, None

    for k in range(age_idx - 1, max(-1, age_idx - 4), -1):
        g = normalize_gender(tokens[k])
        if g:
            gender = g
            gender_idx = k
            break

    return age, age_idx, gender, gender_idx


def split_name_relative(tokens: List[str]) -> Tuple[str, str, Optional[str]]:
    if not tokens:
        return "", "", None

    candidates: List[Tuple[int, str]] = []
    for i, tok in enumerate(tokens):
        if i < 1 or i > len(tokens) - 2:
            continue
        code = normalize_relation_code(tok)
        if code:
            candidates.append((i, code))

    marker_idx = None
    relation_code = None
    if candidates:
        target = len(tokens) * 0.45
        best = min(candidates, key=lambda x: abs(x[0] - target))
        marker_idx = best[0]
        relation_code = best[1]

    if marker_idx is not None:
        left = tokens[:marker_idx]
        right = tokens[marker_idx + 1 :]
    else:
        first = normalize_token(tokens[0])
        rep_idx = None
        for i in range(2, len(tokens)):
            if normalize_token(tokens[i]) == first:
                rep_idx = i
                break
        if rep_idx is not None:
            left = tokens[:rep_idx]
            right = tokens[rep_idx:]
        else:
            mid = len(tokens) // 2
            left = tokens[:mid]
            right = tokens[mid:]

    noise = {
        "wart", "bet", "fart", "pay", "pt", "te", "pw", "ss", "ee", "ase", "nse",
        "array", "ara", "ret", "war", "wag", "wer", "opt", "rhs", "den",
    }

    def clean_name_part(part: List[str]) -> str:
        cleaned: List[str] = []
        for tok in part:
            t = normalize_token(tok)
            if not t:
                continue
            if t.lower() in noise:
                continue
            if not contains_devanagari(t):
                continue
            cleaned.append(t)
        return clean_text(" ".join(cleaned))

    return clean_name_part(left), clean_name_part(right), relation_code


def record_quality(rec: Dict) -> int:
    score = 0
    if rec.get("name"):
        score += 3
    if rec.get("relative_name"):
        score += 2
    if rec.get("gender"):
        score += 2
    if rec.get("relation_code"):
        score += 2
    if rec.get("voter_id"):
        score += 2
    if rec.get("age") is not None:
        score += 1
    return score


def is_valid_house_no(house_no: Optional[str]) -> bool:
    if not house_no:
        return False
    h = normalize_house_no(house_no)
    if not h:
        return False
    return bool(re.match(r"^[0-9]{1,4}([/-][0-9]{1,4})?$", h))


def clean_name_tokens(tokens: List[str]) -> str:
    cleaned: List[str] = []
    noise = {
        "wart", "bet", "fart", "pay", "pt", "te", "pw", "ss", "ee", "ase", "nse",
        "array", "ara", "ret", "war", "wag", "wer", "opt", "rhs", "den",
    }
    for tok in tokens:
        t = normalize_token(tok)
        if not t:
            continue
        if t.lower() in noise:
            continue
        if not contains_devanagari(t):
            continue
        cleaned.append(t)
    return clean_text(" ".join(cleaned))


def build_relaxed_record(
    serial_no: int,
    tokens: List[str],
    source_file: str,
    constants: Dict[str, Optional[str]],
) -> Optional[Dict]:
    if len(tokens) < 4:
        return None

    house_no = None
    body = tokens
    if re.match(r"^[0-9A-Za-z$./-]{1,8}$", tokens[0]):
        cand_house = normalize_house_no(tokens[0])
        if is_valid_house_no(cand_house):
            house_no = cand_house
            body = tokens[1:]

    if len(body) < 3:
        return None

    age = None
    age_idx = None
    for i in range(len(body) - 1, -1, -1):
        parsed_age = parse_age_from_token(body[i])
        if parsed_age is not None:
            age = parsed_age
            age_idx = i
            break
    if age is None or age_idx is None:
        return None

    gender = None
    for i in range(max(0, age_idx - 3), min(len(body), age_idx + 2)):
        g = normalize_gender(body[i])
        if g:
            gender = g
            break

    relation_code = None
    rel_idx = None
    for i in range(1, max(1, age_idx)):
        rc = normalize_relation_code(body[i])
        if rc:
            relation_code = rc
            rel_idx = i
            break

    upto = body[:age_idx]
    if rel_idx is not None and rel_idx < len(upto):
        name_tokens = upto[:rel_idx]
        rel_tokens = upto[rel_idx + 1 :]
    else:
        mid = len(upto) // 2
        name_tokens = upto[:mid]
        rel_tokens = upto[mid:]

    name = clean_name_tokens(name_tokens)
    relative_name = clean_name_tokens(rel_tokens)
    if not name or not contains_devanagari(name):
        return None

    if relation_code is None and gender:
        if gender == MR_MALE:
            relation_code = REL_CODE_FATHER
        elif gender == MR_FEMALE:
            relation_code = REL_CODE_HUSBAND

    voter_id = None
    for tok in body[age_idx + 1 :]:
        d = ascii_digits_only(tok)
        if len(d) >= 5:
            voter_id = d
            break

    rec = {
        "serial_no": serial_no,
        "house_no": house_no,
        "name": name,
        "relative_name": relative_name or None,
        "relation_code": relation_code,
        "relation_code_source": "relaxed",
        "gender": gender,
        "age": age,
        "voter_id": voter_id,
        "source_file": source_file,
    }
    rec.update(constants)
    if record_quality(rec) < 4:
        return None
    return rec


def parse_voter_data(text: str, source_file: str, constants: Dict[str, Optional[str]]) -> List[Dict]:
    best_by_serial: Dict[int, Dict] = {}
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    expected_total = None
    if constants.get("lit_total_voters"):
        d = ascii_digits_only(str(constants["lit_total_voters"]))
        if d.isdigit():
            expected_total = int(d)

    candidate_lines = merge_record_lines(lines, max_extra_lines=2)
    if not candidate_lines:
        candidate_lines = lines

    serial_rows: List[Tuple[int, str]] = []
    prev_serial = None
    for line in candidate_lines:
        m = SERIAL_LINE_RE.match(line)
        if not m:
            continue

        serial_no = correct_serial_number(
            raw_serial=m.group(1),
            prev_serial=prev_serial,
            expected_total=expected_total,
        )
        if serial_no is None or serial_no <= 0:
            continue

        rest = m.group(2).strip()
        if not rest:
            continue
        serial_rows.append((serial_no, rest))
        if prev_serial is None or serial_no > prev_serial:
            prev_serial = serial_no

    for serial_no, rest in serial_rows:
        tokens = [normalize_token(t) for t in rest.split() if normalize_token(t)]
        if len(tokens) < 5:
            continue

        house_no = None
        body_tokens = tokens
        if re.match(r"^[0-9A-Za-z$./-]{1,8}$", tokens[0]):
            candidate_house = normalize_house_no(tokens[0])
            if is_valid_house_no(candidate_house):
                house_no = candidate_house
                body_tokens = tokens[1:]
        if len(body_tokens) < 4:
            continue

        age, age_idx, gender, gender_idx = parse_age_id_gender(body_tokens)
        if age is None or age_idx is None:
            continue
        if age < 18 or age > 110:
            continue

        split_end = gender_idx if gender_idx is not None else age_idx
        core = body_tokens[:split_end]
        if len(core) < 2:
            continue

        name, relative_name, relation_code = split_name_relative(core)
        if not name or not contains_devanagari(name):
            continue

        relation_code_source = "missing"
        if relation_code is not None:
            relation_code_source = "parsed"

        if gender is None and relation_code:
            if relation_code == REL_CODE_FATHER:
                gender = MR_MALE
            elif relation_code == REL_CODE_HUSBAND:
                gender = MR_FEMALE

        if relation_code is None and gender:
            if gender == MR_MALE:
                relation_code = REL_CODE_FATHER
                relation_code_source = "inferred_from_gender"
            elif gender == MR_FEMALE:
                relation_code = REL_CODE_HUSBAND
                relation_code_source = "inferred_from_gender"

        voter_id = None
        for tok in body_tokens[age_idx + 1 :]:
            d = ascii_digits_only(tok)
            if len(d) >= 5:
                voter_id = d
                break

        rec = {
            "serial_no": serial_no,
            "house_no": house_no,
            "name": name,
            "relative_name": relative_name or None,
            "relation_code": relation_code,
            "relation_code_source": relation_code_source,
            "gender": gender,
            "age": age,
            "voter_id": voter_id,
            "source_file": source_file,
        }
        rec.update(constants)
        if record_quality(rec) < 5:
            continue

        old = best_by_serial.get(serial_no)
        if old is None or record_quality(rec) > record_quality(old):
            best_by_serial[serial_no] = rec

    # Fallback pass: fill missing serials using relaxed parsing.
    if expected_total:
        missing = set(range(1, expected_total + 1)) - set(best_by_serial.keys())
        if missing:
            for serial_no, rest in serial_rows:
                if serial_no not in missing:
                    continue
                tokens = [normalize_token(t) for t in rest.split() if normalize_token(t)]
                rec = build_relaxed_record(
                    serial_no=serial_no,
                    tokens=tokens,
                    source_file=source_file,
                    constants=constants,
                )
                if rec is None:
                    continue
                old = best_by_serial.get(serial_no)
                if old is None or record_quality(rec) > record_quality(old):
                    best_by_serial[serial_no] = rec
                    if serial_no in missing:
                        missing.remove(serial_no)

    return [best_by_serial[k] for k in sorted(best_by_serial.keys())]


OUTPUT_COLUMNS = [
    "serial_no",
    "house_no",
    "name",
    "relation_code",
    "relative_name",
    "gender",
    "age",
    "voter_id",
    "lit_state_code",
    "lit_ac_no",
    "lit_ac_name",
    "lit_part_no",
    "lit_pc_no",
    "lit_pc_name",
    "lit_reservation",
    "lit_revision_year",
    "lit_qualifying_date",
    "lit_main_town",
    "lit_saja",
    "lit_mandal",
    "lit_tahsil",
    "lit_district",
    "lit_polling_station_no",
    "lit_polling_station_name",
    "lit_total_voters",
]

MARATHI_COLUMN_NAMES = {
    "serial_no": "अनुक्रमांक",
    "house_no": "घर क्रमांक",
    "name": "मतदाराचे पूर्ण नाव",
    "relation_code": "नाते",
    "relative_name": "नातेवाईकाचे पूर्ण नाव",
    "gender": "लिंग",
    "age": "वय",
    "voter_id": "ओळखपत्र क्रमांक",
}

ENGLISH_COLUMN_NAMES = {
    "serial_no": "Serial No",
    "house_no": "House No",
    "name": "Voter Name",
    "relation_code": "Relation (Fat/Fus)",
    "relative_name": "Relative Name",
    "gender": "Gender",
    "age": "Age",
    "voter_id": "Voter ID",
}


def build_output_frames(records: List[Dict]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.DataFrame(records or [], columns=OUTPUT_COLUMNS).reindex(columns=OUTPUT_COLUMNS)
    marathi_df = df.rename(columns=MARATHI_COLUMN_NAMES)

    english_df = df.copy()
    english_df["gender"] = english_df["gender"].apply(to_english_gender)
    english_df["relation_code"] = english_df["relation_code"].apply(to_english_relation_short)
    english_df["name"] = english_df["name"].apply(transliterate_devanagari)
    english_df["relative_name"] = english_df["relative_name"].apply(transliterate_devanagari)
    english_df = english_df.rename(columns=ENGLISH_COLUMN_NAMES)
    return marathi_df, english_df


def save_excel(records: List[Dict], excel_path: Path) -> None:
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    marathi_df, english_df = build_output_frames(records)

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        marathi_df.to_excel(writer, sheet_name="Marathi", index=False)
        english_df.to_excel(writer, sheet_name="English", index=False)

    print(f"Combined Excel written: {excel_path} ({len(marathi_df)} rows, sheets: Marathi + English)")


def save_csv_files(records: List[Dict], csv_dir: Path) -> None:
    csv_dir.mkdir(parents=True, exist_ok=True)
    marathi_df, english_df = build_output_frames(records)
    marathi_path = csv_dir / "all_voters_marathi.csv"
    english_path = csv_dir / "all_voters_english.csv"
    marathi_df.to_csv(marathi_path, index=False, encoding="utf-8-sig")
    english_df.to_csv(english_path, index=False, encoding="utf-8-sig")
    print(f"CSV written: {marathi_path} ({len(marathi_df)} rows)")
    print(f"CSV written: {english_path} ({len(english_df)} rows)")


def save_mongo(records: List[Dict], mongo_uri: str, mongo_db: str, mongo_collection: str) -> None:
    if not mongo_uri:
        print("MongoDB skipped (no --mongo-uri provided).")
        return
    if not records:
        print("MongoDB skipped (no records).")
        return
    if MongoClient is None or UpdateOne is None:
        raise RuntimeError("pymongo is required. Install: pip install pymongo")

    client = MongoClient(mongo_uri)
    collection = client[mongo_db][mongo_collection]
    collection.create_index([("source_file", 1), ("serial_no", 1)], unique=True)

    ops = []
    for r in records:
        ops.append(
            UpdateOne(
                {"source_file": r["source_file"], "serial_no": r["serial_no"]},
                {"$set": r},
                upsert=True,
            )
        )
    result = collection.bulk_write(ops, ordered=False)
    print(
        "MongoDB upsert complete: "
        f"matched={result.matched_count}, modified={result.modified_count}, "
        f"upserted={len(result.upserted_ids)}"
    )


def parse_args():
    p = argparse.ArgumentParser(description="Batch convert voter PDFs to local OCR Excel/CSV files")
    p.add_argument("--input-dir", default="pdfs", help="Folder containing PDF files")
    p.add_argument("--excel", default="output/all_voters.xlsx", help="Output Excel path")
    p.add_argument("--csv-dir", default="output/csv", help="Folder for Marathi and English CSV files")
    p.add_argument(
        "--output-format",
        choices=["xlsx", "csv", "both"],
        default="both",
        help="Write Excel, CSV, or both",
    )
    p.add_argument("--raw-dir", default="output/raw_text", help="Raw OCR text output folder")
    p.add_argument("--lang", default="mar+eng", help="OCR language(s), e.g. mar+eng")
    p.add_argument(
        "--accuracy-mode",
        choices=["fast", "balanced", "high"],
        default="balanced",
        help="OCR effort level: fast (quick), balanced (default), high (best accuracy)",
    )
    p.add_argument("--dpi", type=int, default=300, help="Render DPI (higher = slower, but more accurate)")
    p.add_argument("--max-pages", type=int, default=None, help="Process first N pages per PDF")
    p.add_argument("--mongo-uri", default="", help="Mongo URI (empty = skip)")
    p.add_argument("--mongo-db", default="voter_db", help="Mongo database name")
    p.add_argument("--mongo-collection", default="voters", help="Mongo collection name")
    return p.parse_args()


def main():
    args = parse_args()
    input_dir = Path(args.input_dir)
    raw_dir = Path(args.raw_dir)
    excel_path = Path(args.excel)
    csv_dir = Path(args.csv_dir)

    if not input_dir.exists():
        raise FileNotFoundError(f"Input folder not found: {input_dir}")

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in: {input_dir}")

    raw_dir.mkdir(parents=True, exist_ok=True)
    configure_tesseract()
    configure_tessdata_prefix()
    ocr_lang = resolve_ocr_lang(args.lang)

    all_records: List[Dict] = []
    print(f"Found {len(pdf_files)} PDF files.")

    for idx, pdf in enumerate(pdf_files, start=1):
        print(f"[{idx}/{len(pdf_files)}] Processing: {pdf.name}")
        images = pdf_to_images(pdf, dpi=args.dpi, max_pages=args.max_pages)
        text = extract_text(images, ocr_lang=ocr_lang, accuracy_mode=args.accuracy_mode)

        raw_file = raw_dir / f"{pdf.stem}.txt"
        raw_file.write_text(text, encoding="utf-8")

        constants = extract_constants(text, source_file=pdf.name)
        records = parse_voter_data(text, source_file=pdf.name, constants=constants)
        print(f"      Parsed records: {len(records)}")
        all_records.extend(records)

    if not all_records:
        print("Warning: No voter records parsed. Output files will contain headers only.")

    if args.output_format in {"xlsx", "both"}:
        save_excel(all_records, excel_path)
    if args.output_format in {"csv", "both"}:
        save_csv_files(all_records, csv_dir)
    save_mongo(
        all_records,
        mongo_uri=args.mongo_uri,
        mongo_db=args.mongo_db,
        mongo_collection=args.mongo_collection,
    )
    print("Done.")


if __name__ == "__main__":
    main()
