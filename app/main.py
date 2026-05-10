import io
import json
import logging
import math
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from io import BytesIO
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import run_batch

try:
    from bson.binary import Binary
except ImportError:
    Binary = None

try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None


BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = BASE_DIR / "output"
JOB_ROOT = OUTPUT_ROOT / "jobs"

MAX_FILES_PER_JOB = 50
ALLOWED_ACCURACY = {"fast", "balanced", "high"}
ALLOWED_SHEET = {"marathi", "english"}

_INF = float("inf")
_NEG_INF = float("-inf")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB", "pdf2excel")
MONGO_TIMEOUT_MS = env_int("MONGO_TIMEOUT_MS", 5000)
MONGO_STORE_OUTPUT_FILES = os.getenv("MONGO_STORE_OUTPUT_FILES", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTH_USERNAME = os.getenv("APP_AUTH_USER", "datainteg")
AUTH_PASSWORD = os.getenv("APP_AUTH_PASS", "Welcome@911")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-session-secret")
MAX_PREVIEW_ROWS = max(20, min(env_int("MAX_PREVIEW_ROWS", 100), 500))

mongo_client = None
mongo_db = None
mongo_jobs = None
mongo_files = None
mongo_voters = None  # parsed voter records collection
logger = logging.getLogger(__name__)


def _sanitize(obj: Any) -> Any:
    """Recursively replace non-JSON-compliant floats (NaN, inf) with None."""
    if isinstance(obj, float):
        if obj != obj or obj == _INF or obj == _NEG_INF:
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


@asynccontextmanager
async def lifespan(application: FastAPI):
    ensure_dirs()
    ensure_required_templates()
    run_batch.configure_tesseract()
    run_batch.configure_tessdata_prefix()
    init_mongo()
    yield


app = FastAPI(title="PDF to Excel OCR", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    JOB_ROOT.mkdir(parents=True, exist_ok=True)


def ensure_required_templates() -> None:
    required = ("login.html", "index.html")
    missing = [name for name in required if not (BASE_DIR / "templates" / name).exists()]
    if missing:
        raise RuntimeError(f"Missing template files: {', '.join(missing)}")


def is_authenticated(request: Request) -> bool:
    return request.session.get("auth_user") == AUTH_USERNAME


def require_api_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized. Please login.")


def template_ctx(request: Request, extra: Optional[Dict] = None) -> Dict:
    data = {"request": request, "auth_user": request.session.get("auth_user")}
    if extra:
        data.update(extra)
    return data


def render_template_response(
    request: Request,
    name: str,
    extra: Optional[Dict] = None,
    status_code: int = 200,
):
    context = template_ctx(request, extra)
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
    )


def mongo_enabled() -> bool:
    return mongo_jobs is not None and mongo_files is not None and Binary is not None


def init_mongo() -> None:
    global mongo_client, mongo_db, mongo_jobs, mongo_files, mongo_voters
    if not MONGO_URI:
        return
    if MongoClient is None or Binary is None:
        print("MongoDB disabled: pymongo/bson not available.")
        return

    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
        mongo_client.admin.command("ping")
        mongo_db = mongo_client[MONGO_DB_NAME]
        mongo_jobs = mongo_db["jobs"]
        mongo_files = mongo_db["job_files"]
        mongo_voters = mongo_db["voters"]
        mongo_jobs.create_index("job_id", unique=True)
        mongo_files.create_index([("job_id", 1), ("file_key", 1)], unique=True)
        mongo_voters.create_index([("job_id", 1), ("serial_no", 1)], unique=True)
        mongo_voters.create_index("job_id")
        print(f"MongoDB connected: {MONGO_URI} db={MONGO_DB_NAME}")
    except Exception as exc:
        print(f"MongoDB disabled: {exc}")
        mongo_client = None
        mongo_db = None
        mongo_jobs = None
        mongo_files = None
        mongo_voters = None


def validate_job_id(job_id: str) -> None:
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        raise HTTPException(status_code=404, detail="Job not found.")


def get_job_dir(job_id: str) -> Path:
    validate_job_id(job_id)
    job_dir = JOB_ROOT / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found.")
    return job_dir


def safe_pdf_filename(filename: str, index: int) -> str:
    raw = Path(filename or f"file_{index}.pdf").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not cleaned:
        cleaned = f"file_{index}.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    stem = Path(cleaned).stem[:90] or f"file_{index}"
    return f"{stem}.pdf"


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.where(pd.notna(df), None)
    return df.replace([_INF, _NEG_INF], None)


def dataframe_rows(df: pd.DataFrame, limit: int) -> List[Dict]:
    if df.empty:
        return []
    return _clean_df(df.head(limit).copy()).to_dict(orient="records")


def paged_preview_from_df(df: pd.DataFrame, page: int, page_size: int) -> Dict:
    total_rows = len(df)
    total_pages = max(1, math.ceil(total_rows / page_size)) if page_size > 0 else 1
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    part = _clean_df(df.iloc[start:end].copy())

    return {
        "columns": list(df.columns),
        "rows": part.to_dict(orient="records"),
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
    }


def csv_preview(csv_path: Path, page: int, page_size: int) -> Dict:
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="Preview file not found.")
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    return paged_preview_from_df(df, page=page, page_size=page_size)


def csv_preview_from_bytes(csv_data: bytes, page: int, page_size: int) -> Dict:
    df = pd.read_csv(BytesIO(csv_data), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return paged_preview_from_df(df, page=page, page_size=page_size)


def write_job_meta(job_dir: Path, payload: Dict) -> None:
    meta_path = job_dir / "job.json"
    meta_path.write_text(
        json.dumps(_sanitize(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def make_artifact(file_key: str, label: str, download_url: str) -> Dict:
    return {
        "file_key": file_key,
        "label": label,
        "download_url": download_url,
        "available": False,
        "storage": "none",
        "size_bytes": None,
        "error": None,
    }


def exc_message(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text[:500]
    return exc.__class__.__name__


def mark_artifact_from_disk(artifact: Dict, path: Path, default_error: str) -> None:
    if path.exists():
        artifact["available"] = True
        artifact["storage"] = "disk"
        artifact["size_bytes"] = int(path.stat().st_size)
        artifact["error"] = None
    elif not artifact.get("error"):
        artifact["error"] = default_error


def mongo_save_file(
    job_id: str,
    file_key: str,
    file_path: Path,
    content_type: str,
    download_name: str,
) -> Dict:
    result = {"stored": False, "error": None}
    if not mongo_enabled():
        result["error"] = "MongoDB not enabled."
        return result
    if not file_path.exists():
        result["error"] = "File not found on disk."
        return result

    try:
        data = file_path.read_bytes()
        mongo_files.update_one(
            {"job_id": job_id, "file_key": file_key},
            {
                "$set": {
                    "job_id": job_id,
                    "file_key": file_key,
                    "filename": download_name,
                    "content_type": content_type,
                    "data": Binary(data),
                    "size": len(data),
                    "updated_at": utc_now_iso(),
                }
            },
            upsert=True,
        )
        result["stored"] = True
        return result
    except Exception as exc:
        result["error"] = exc_message(exc)
        return result


def store_job_in_mongo(
    payload: Dict,
    excel_path: Path,
    marathi_csv_path: Path,
    english_csv_path: Path,
) -> Dict:
    result = {
        "enabled": mongo_enabled(),
        "job_saved": False,
        "job_error": None,
        "files": {
            "excel": {"stored": False, "error": None},
            "csv_marathi": {"stored": False, "error": None},
            "csv_english": {"stored": False, "error": None},
        },
    }

    if not mongo_enabled():
        return result

    job_id = payload["job_id"]
    try:
        mongo_jobs.update_one(
            {"job_id": job_id},
            {"$set": payload},
            upsert=True,
        )
        result["job_saved"] = True
    except Exception as exc:
        result["job_error"] = exc_message(exc)
        return result

    if not MONGO_STORE_OUTPUT_FILES:
        result["files"]["excel"]["error"] = "Mongo file storage disabled by config."
        result["files"]["csv_marathi"]["error"] = "Mongo file storage disabled by config."
        result["files"]["csv_english"]["error"] = "Mongo file storage disabled by config."
        return result

    result["files"]["excel"] = mongo_save_file(
        job_id=job_id,
        file_key="excel",
        file_path=excel_path,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        download_name=f"{job_id}_all_voters.xlsx",
    )
    result["files"]["csv_marathi"] = mongo_save_file(
        job_id=job_id,
        file_key="csv_marathi",
        file_path=marathi_csv_path,
        content_type="text/csv",
        download_name=f"{job_id}_all_voters_marathi.csv",
    )
    result["files"]["csv_english"] = mongo_save_file(
        job_id=job_id,
        file_key="csv_english",
        file_path=english_csv_path,
        content_type="text/csv",
        download_name=f"{job_id}_all_voters_english.csv",
    )
    return result


def artifact_output_status(artifacts: Dict[str, Dict]) -> str:
    available = sum(1 for item in artifacts.values() if item.get("available"))
    total = len(artifacts)
    if total == 0:
        return "completed"
    if available == total:
        return "completed"
    if available > 0:
        return "completed_with_warnings"
    return "failed_outputs"


def preview_block(columns: List[str], rows: List[Dict], truncated: bool) -> Dict:
    return {"columns": columns, "rows": rows, "truncated": truncated}


def mongo_find_job(job_id: str) -> Optional[Dict]:
    if not mongo_enabled():
        return None
    doc = mongo_jobs.find_one({"job_id": job_id}, {"_id": 0})
    return doc


def mongo_find_file(job_id: str, file_key: str) -> Optional[Dict]:
    if not mongo_enabled():
        return None
    doc = mongo_files.find_one(
        {"job_id": job_id, "file_key": file_key},
        {"_id": 0, "filename": 1, "content_type": 1, "data": 1},
    )
    return doc


def mongo_file_response(doc: Dict) -> Response:
    content = bytes(doc["data"])
    filename = doc.get("filename", "download.bin")
    content_type = doc.get("content_type", "application/octet-stream")
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def mongo_store_voters(job_id: str, records: List[Dict]) -> Optional[str]:
    """Bulk upsert parsed voter records into voters collection. Returns error string or None."""
    if mongo_voters is None or not records:
        return None
    try:
        from pymongo import UpdateOne as _UpdateOne
        ops = [
            _UpdateOne(
                {"job_id": job_id, "serial_no": r["serial_no"]},
                {"$set": {**r, "job_id": job_id}},
                upsert=True,
            )
            for r in records
        ]
        mongo_voters.bulk_write(ops, ordered=False)
        return None
    except Exception as exc:
        return exc_message(exc)


def mongo_get_voters(job_id: str, page: int, page_size: int, sheet: str) -> Optional[Dict]:
    """Return paginated voter records from MongoDB, or None if unavailable."""
    if mongo_voters is None:
        return None
    try:
        total = mongo_voters.count_documents({"job_id": job_id})
        if total == 0:
            return None
        total_pages = max(1, math.ceil(total / page_size))
        page = max(1, min(page, total_pages))
        skip = (page - 1) * page_size
        docs = list(
            mongo_voters.find(
                {"job_id": job_id},
                {"_id": 0, "job_id": 0},
            )
            .sort("serial_no", 1)
            .skip(skip)
            .limit(page_size)
        )
        if sheet == "english":
            docs = _records_to_english(docs)
        columns = run_batch.OUTPUT_COLUMNS
        return {
            "columns": columns,
            "rows": [_sanitize({c: r.get(c) for c in columns}) for r in docs],
            "page": page,
            "page_size": page_size,
            "total_rows": total,
            "total_pages": total_pages,
            "sheet": sheet,
            "source": "mongo",
        }
    except Exception as exc:
        logger.warning("mongo_get_voters failed: %s", exc_message(exc))
        return None


def _records_to_english(records: List[Dict]) -> List[Dict]:
    out = []
    for r in records:
        e = dict(r)
        e["gender"] = run_batch.to_english_gender(e.get("gender"))
        e["relation_code"] = run_batch.to_english_relation_short(e.get("relation_code"))
        e["name"] = run_batch.transliterate_devanagari(e.get("name"))
        e["relative_name"] = run_batch.transliterate_devanagari(e.get("relative_name"))
        out.append(e)
    return out


def records_to_dataframe(job_id: str, sheet: str) -> Optional[pd.DataFrame]:
    """Build DataFrame from MongoDB voter records for a job."""
    if mongo_voters is None:
        return None
    try:
        docs = list(
            mongo_voters.find({"job_id": job_id}, {"_id": 0, "job_id": 0}).sort("serial_no", 1)
        )
        if not docs:
            return None
        if sheet == "english":
            docs = _records_to_english(docs)
        df = pd.DataFrame(docs, columns=run_batch.OUTPUT_COLUMNS).reindex(columns=run_batch.OUTPUT_COLUMNS)
        if sheet == "english":
            df = df.rename(columns=run_batch.ENGLISH_COLUMN_NAMES)
        else:
            df = df.rename(columns=run_batch.MARATHI_COLUMN_NAMES)
        return df
    except Exception as exc:
        logger.warning("records_to_dataframe failed: %s", exc_message(exc))
        return None


def read_job_meta(job_id: str) -> Dict:
    validate_job_id(job_id)
    mongo_doc = mongo_find_job(job_id)
    if mongo_doc is not None:
        return mongo_doc

    job_dir = get_job_dir(job_id)
    meta_path = job_dir / "job.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Job metadata not found.")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def job_summary(meta: Dict) -> Dict:
    output = meta.get("output", {}) if isinstance(meta, dict) else {}
    input_data = meta.get("input", {}) if isinstance(meta, dict) else {}
    artifacts = output.get("artifacts", {}) if isinstance(output, dict) else {}
    artifact_list = []
    if isinstance(artifacts, dict):
        for item in artifacts.values():
            if isinstance(item, dict):
                artifact_list.append(
                    {
                        "file_key": item.get("file_key"),
                        "label": item.get("label"),
                        "available": bool(item.get("available")),
                        "download_url": item.get("download_url"),
                    }
                )

    return {
        "job_id": meta.get("job_id"),
        "status": meta.get("status", "unknown"),
        "created_at": meta.get("created_at"),
        "total_files": int(input_data.get("total_files", 0) or 0),
        "total_records": int(output.get("total_records", 0) or 0),
        "artifacts": artifact_list,
    }


def list_recent_jobs(limit: int = 10) -> List[Dict]:
    jobs: List[Dict] = []
    limit = max(1, min(limit, 50))

    if mongo_enabled():
        try:
            cursor = mongo_jobs.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
            jobs = [job_summary(doc) for doc in cursor]
            if jobs:
                return jobs
        except Exception as exc:
            logger.warning("recent jobs from mongo failed: %s", exc_message(exc))

    if not JOB_ROOT.exists():
        return jobs

    records: List[Dict] = []
    for meta_path in JOB_ROOT.glob("*/job.json"):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            payload["_mtime"] = meta_path.stat().st_mtime
            records.append(payload)
        except Exception:
            continue

    records.sort(key=lambda item: item.get("_mtime", 0), reverse=True)
    return [job_summary(item) for item in records[:limit]]


def process_uploaded_pdfs(
    files: List[UploadFile],
    lang: str,
    accuracy_mode: str,
    dpi: int,
    max_pages: Optional[int],
) -> Dict:
    if not files:
        raise HTTPException(status_code=400, detail="Please upload at least one PDF.")
    if len(files) > MAX_FILES_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Max allowed per request: {MAX_FILES_PER_JOB}.",
        )
    if accuracy_mode not in ALLOWED_ACCURACY:
        raise HTTPException(status_code=400, detail="Invalid accuracy mode.")
    if dpi < 120 or dpi > 600:
        raise HTTPException(status_code=400, detail="DPI should be between 120 and 600.")
    if max_pages is not None and max_pages <= 0:
        max_pages = None

    for upload in files:
        if not (upload.filename or "").lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only .pdf files are allowed.")

    job_id = uuid.uuid4().hex[:12]
    job_started = time.time()
    job_dir = JOB_ROOT / job_id
    pdf_dir = job_dir / "pdfs"
    raw_dir = job_dir / "raw_text"
    csv_dir = job_dir / "csv"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    csv_dir.mkdir(parents=True, exist_ok=True)

    saved_files: List[Path] = []
    for i, upload in enumerate(files, start=1):
        name = safe_pdf_filename(upload.filename or "", i)
        target = pdf_dir / name
        dedupe = 1
        while target.exists():
            target = pdf_dir / f"{Path(name).stem}_{dedupe}.pdf"
            dedupe += 1

        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_files.append(target)
        upload.file.close()

    ocr_lang = run_batch.resolve_ocr_lang(lang)
    print(
        f"[job {job_id}] started files={len(saved_files)} "
        f"lang={ocr_lang} accuracy={accuracy_mode} dpi={dpi} max_pages={max_pages}"
    )

    all_records: List[Dict] = []
    per_file: List[Dict] = []

    for file_idx, pdf in enumerate(saved_files, start=1):
        file_started = time.time()
        print(f"[job {job_id}] file {file_idx}/{len(saved_files)} start: {pdf.name}")
        images = run_batch.pdf_to_images(pdf, dpi=dpi, max_pages=max_pages)
        print(f"[job {job_id}] file {file_idx}/{len(saved_files)} pages={len(images)}")
        text = run_batch.extract_text(images, ocr_lang=ocr_lang, accuracy_mode=accuracy_mode)

        raw_file = raw_dir / f"{pdf.stem}.txt"
        raw_file.write_text(text, encoding="utf-8")

        constants = run_batch.extract_constants(text, source_file=pdf.name)
        records = run_batch.parse_voter_data(text, source_file=pdf.name, constants=constants)
        all_records.extend(records)
        file_elapsed = time.time() - file_started
        print(
            f"[job {job_id}] file {file_idx}/{len(saved_files)} done "
            f"records={len(records)} elapsed={file_elapsed:.1f}s"
        )
        per_file.append(
            {
                "file_name": pdf.name,
                "pages_processed": len(images),
                "records_parsed": len(records),
            }
        )

    excel_path = job_dir / "all_voters.xlsx"
    marathi_csv_path = csv_dir / "all_voters_marathi.csv"
    english_csv_path = csv_dir / "all_voters_english.csv"
    artifacts = {
        "excel": make_artifact(
            file_key="excel",
            label="Excel (Marathi + English)",
            download_url=f"/api/jobs/{job_id}/download/excel",
        ),
        "csv_marathi": make_artifact(
            file_key="csv_marathi",
            label="Marathi CSV",
            download_url=f"/api/jobs/{job_id}/download/csv/marathi",
        ),
        "csv_english": make_artifact(
            file_key="csv_english",
            label="English CSV",
            download_url=f"/api/jobs/{job_id}/download/csv/english",
        ),
    }
    warnings: List[str] = []

    print(f"[job {job_id}] writing outputs excel/csv ...")
    marathi_df: Optional[pd.DataFrame] = None
    english_df: Optional[pd.DataFrame] = None
    try:
        marathi_df, english_df = run_batch.build_output_frames(all_records)
    except Exception as exc:
        frame_error = f"Output frame build failed: {exc_message(exc)}"
        artifacts["excel"]["error"] = frame_error
        artifacts["csv_marathi"]["error"] = frame_error
        artifacts["csv_english"]["error"] = frame_error

    if marathi_df is not None and english_df is not None:
        try:
            excel_path.parent.mkdir(parents=True, exist_ok=True)
            with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
                marathi_df.to_excel(writer, sheet_name="Marathi", index=False)
                english_df.to_excel(writer, sheet_name="English", index=False)
        except Exception as exc:
            artifacts["excel"]["error"] = f"Excel generation failed: {exc_message(exc)}"

        try:
            csv_dir.mkdir(parents=True, exist_ok=True)
            marathi_df.to_csv(marathi_csv_path, index=False, encoding="utf-8-sig")
        except Exception as exc:
            artifacts["csv_marathi"]["error"] = f"Marathi CSV generation failed: {exc_message(exc)}"

        try:
            csv_dir.mkdir(parents=True, exist_ok=True)
            english_df.to_csv(english_csv_path, index=False, encoding="utf-8-sig")
        except Exception as exc:
            artifacts["csv_english"]["error"] = f"English CSV generation failed: {exc_message(exc)}"

    mark_artifact_from_disk(
        artifact=artifacts["excel"],
        path=excel_path,
        default_error="Excel file was not created.",
    )
    mark_artifact_from_disk(
        artifact=artifacts["csv_marathi"],
        path=marathi_csv_path,
        default_error="Marathi CSV file was not created.",
    )
    mark_artifact_from_disk(
        artifact=artifacts["csv_english"],
        path=english_csv_path,
        default_error="English CSV file was not created.",
    )

    preview_marathi = preview_block(columns=[], rows=[], truncated=False)
    preview_english = preview_block(columns=[], rows=[], truncated=False)
    if marathi_df is not None and english_df is not None:
        preview_marathi = preview_block(
            columns=list(marathi_df.columns),
            rows=dataframe_rows(marathi_df, MAX_PREVIEW_ROWS),
            truncated=len(marathi_df) > MAX_PREVIEW_ROWS,
        )
        preview_english = preview_block(
            columns=list(english_df.columns),
            rows=dataframe_rows(english_df, MAX_PREVIEW_ROWS),
            truncated=len(english_df) > MAX_PREVIEW_ROWS,
        )

    for key, info in artifacts.items():
        if info["error"]:
            warnings.append(f"{key}: {info['error']}")

    payload = {
        "job_id": job_id,
        "status": artifact_output_status(artifacts),
        "created_at": utc_now_iso(),
        "options": {
            "lang": ocr_lang,
            "accuracy_mode": accuracy_mode,
            "dpi": dpi,
            "max_pages": max_pages,
        },
        "input": {
            "total_files": len(saved_files),
            "file_names": [p.name for p in saved_files],
        },
        "output": {
            "total_records": len(all_records),
            "per_file": per_file,
            "download_excel_url": f"/api/jobs/{job_id}/download/excel",
            "download_marathi_csv_url": f"/api/jobs/{job_id}/download/csv/marathi",
            "download_english_csv_url": f"/api/jobs/{job_id}/download/csv/english",
            "preview_url": f"/api/jobs/{job_id}/preview",
            "artifacts": artifacts,
            "warnings": warnings,
        },
        "preview": {
            "marathi": preview_marathi,
            "english": preview_english,
        },
    }

    voter_err = mongo_store_voters(job_id, all_records)
    if voter_err:
        warnings.append(f"mongo_voters: {voter_err}")

    mongo_store = store_job_in_mongo(
        payload=payload,
        excel_path=excel_path,
        marathi_csv_path=marathi_csv_path,
        english_csv_path=english_csv_path,
    )
    if mongo_store.get("enabled"):
        if mongo_store.get("job_error"):
            warnings.append(f"mongo_meta: {mongo_store['job_error']}")
        for file_key, mongo_result in mongo_store.get("files", {}).items():
            artifact = artifacts.get(file_key)
            if not artifact:
                continue
            if mongo_result.get("stored"):
                artifact["storage"] = "disk+mongo" if artifact["available"] else "mongo"
            elif mongo_result.get("error"):
                warnings.append(f"mongo_{file_key}: {mongo_result['error']}")

    payload["status"] = artifact_output_status(artifacts)
    write_job_meta(job_dir, payload)
    if mongo_store.get("enabled") and mongo_store.get("job_saved"):
        try:
            mongo_jobs.update_one({"job_id": job_id}, {"$set": _sanitize(payload)})
        except Exception as exc:
            warnings.append(f"mongo_meta_refresh: {exc_message(exc)}")

    job_elapsed = time.time() - job_started
    print(
        f"[job {job_id}] completed total_records={len(all_records)} "
        f"status={payload['status']} elapsed={job_elapsed:.1f}s"
    )
    return _sanitize(payload)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled server error path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please check server logs and retry."},
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return render_template_response(request=request, name="login.html")


@app.post("/login")
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if username == AUTH_USERNAME and password == AUTH_PASSWORD:
        request.session["auth_user"] = username
        return RedirectResponse(url="/", status_code=303)
    return render_template_response(
        request=request,
        name="login.html",
        extra={"error": "Invalid username or password."},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return render_template_response(request=request, name="index.html")


@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> Dict[str, str]:
    return {"status": "ok", "mongo": "enabled" if mongo_enabled() else "disabled"}


@app.post("/api/process")
def process(
    request: Request,
    files: List[UploadFile] = File(...),
    lang: str = Form("mar+eng"),
    accuracy_mode: str = Form("balanced"),
    dpi: int = Form(300),
    max_pages: Optional[int] = Form(None),
):
    require_api_auth(request)
    try:
        return process_uploaded_pdfs(
            files=files,
            lang=lang,
            accuracy_mode=accuracy_mode,
            dpi=dpi,
            max_pages=max_pages,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Processing failed: {exc}") from exc


@app.get("/api/jobs/recent")
def get_recent_jobs(request: Request, limit: int = Query(8, ge=1, le=50)):
    require_api_auth(request)
    return {"jobs": list_recent_jobs(limit=limit)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    require_api_auth(request)
    return read_job_meta(job_id)


@app.get("/api/jobs/{job_id}/preview")
def get_preview(
    job_id: str,
    request: Request,
    sheet: str = Query("marathi"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    require_api_auth(request)
    meta = read_job_meta(job_id)
    sheet_key = sheet.lower()
    if sheet_key not in ALLOWED_SHEET:
        raise HTTPException(status_code=400, detail="Invalid sheet. Use marathi or english.")

    filename = "all_voters_marathi.csv" if sheet_key == "marathi" else "all_voters_english.csv"
    file_key = "csv_marathi" if sheet_key == "marathi" else "csv_english"
    artifacts = meta.get("output", {}).get("artifacts", {})
    artifact = artifacts.get(file_key, {})
    if artifact and not artifact.get("available"):
        raise HTTPException(
            status_code=404,
            detail=artifact.get("error") or "Preview source CSV is not available.",
        )

    mongo_doc = mongo_find_file(job_id=job_id, file_key=file_key)
    if mongo_doc is not None:
        payload = csv_preview_from_bytes(
            csv_data=bytes(mongo_doc["data"]),
            page=page,
            page_size=page_size,
        )
        payload["sheet"] = sheet_key
        return payload

    job_dir = get_job_dir(job_id)
    csv_path = job_dir / "csv" / filename
    payload = csv_preview(csv_path=csv_path, page=page, page_size=page_size)
    payload["sheet"] = sheet_key
    return payload


@app.get("/api/jobs/{job_id}/download/excel")
def download_excel(job_id: str, request: Request):
    require_api_auth(request)
    meta = read_job_meta(job_id)
    artifact = meta.get("output", {}).get("artifacts", {}).get("excel", {})
    if artifact and not artifact.get("available"):
        raise HTTPException(
            status_code=404,
            detail=artifact.get("error") or "Excel output is not available.",
        )
    mongo_doc = mongo_find_file(job_id=job_id, file_key="excel")
    if mongo_doc is not None:
        return mongo_file_response(mongo_doc)

    job_dir = get_job_dir(job_id)
    excel_path = job_dir / "all_voters.xlsx"
    if not excel_path.exists():
        raise HTTPException(status_code=404, detail="Excel output not found.")
    return FileResponse(
        path=excel_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{job_id}_all_voters.xlsx",
    )


@app.get("/api/jobs/{job_id}/download/csv/{sheet}")
def download_csv(job_id: str, sheet: str, request: Request):
    require_api_auth(request)
    meta = read_job_meta(job_id)
    sheet_key = sheet.lower()
    if sheet_key not in ALLOWED_SHEET:
        raise HTTPException(status_code=400, detail="Invalid sheet. Use marathi or english.")

    file_key = "csv_marathi" if sheet_key == "marathi" else "csv_english"
    artifact = meta.get("output", {}).get("artifacts", {}).get(file_key, {})
    if artifact and not artifact.get("available"):
        raise HTTPException(
            status_code=404,
            detail=artifact.get("error") or "CSV output is not available.",
        )
    mongo_doc = mongo_find_file(job_id=job_id, file_key=file_key)
    if mongo_doc is not None:
        return mongo_file_response(mongo_doc)

    job_dir = get_job_dir(job_id)
    filename = "all_voters_marathi.csv" if sheet_key == "marathi" else "all_voters_english.csv"
    csv_path = job_dir / "csv" / filename
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="CSV output not found.")
    return FileResponse(
        path=csv_path,
        media_type="text/csv",
        filename=f"{job_id}_{filename}",
    )


@app.get("/api/jobs/{job_id}/records")
def get_records(
    job_id: str,
    request: Request,
    sheet: str = Query("marathi"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    """Return paginated parsed voter records from MongoDB (live data, not CSV)."""
    require_api_auth(request)
    validate_job_id(job_id)
    sheet_key = sheet.lower()
    if sheet_key not in ALLOWED_SHEET:
        raise HTTPException(status_code=400, detail="Invalid sheet. Use marathi or english.")

    result = mongo_get_voters(job_id, page=page, page_size=page_size, sheet=sheet_key)
    if result is None:
        raise HTTPException(status_code=404, detail="No voter records found for this job in MongoDB.")
    return result


@app.patch("/api/jobs/{job_id}/records/{serial_no}")
def patch_record(
    job_id: str,
    serial_no: int,
    request: Request,
    updates: Dict = Body(...),
):
    """Manually correct a single voter record field(s)."""
    require_api_auth(request)
    validate_job_id(job_id)

    if mongo_voters is None:
        raise HTTPException(status_code=503, detail="MongoDB not available for corrections.")

    EDITABLE_FIELDS = {
        "house_no", "name", "relative_name", "relation_code",
        "gender", "age", "voter_id",
    }
    patch = {k: v for k, v in updates.items() if k in EDITABLE_FIELDS}
    if not patch:
        raise HTTPException(status_code=400, detail=f"No editable fields. Allowed: {sorted(EDITABLE_FIELDS)}")

    patch["_corrected"] = True
    patch["_corrected_at"] = utc_now_iso()

    result = mongo_voters.update_one(
        {"job_id": job_id, "serial_no": serial_no},
        {"$set": patch},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"Record serial_no={serial_no} not found.")

    doc = mongo_voters.find_one({"job_id": job_id, "serial_no": serial_no}, {"_id": 0, "job_id": 0})
    return _sanitize(doc)


@app.get("/api/jobs/{job_id}/export/{sheet}/{fmt}")
def export_from_mongo(job_id: str, sheet: str, fmt: str, request: Request):
    """Generate Excel or CSV on-the-fly from live MongoDB voter records (includes corrections)."""
    require_api_auth(request)
    validate_job_id(job_id)

    sheet_key = sheet.lower()
    if sheet_key not in ALLOWED_SHEET:
        raise HTTPException(status_code=400, detail="Invalid sheet. Use marathi or english.")
    fmt_key = fmt.lower()
    if fmt_key not in {"excel", "csv"}:
        raise HTTPException(status_code=400, detail="Invalid format. Use excel or csv.")

    df = records_to_dataframe(job_id, sheet_key)
    if df is None:
        raise HTTPException(status_code=404, detail="No voter records in MongoDB for this job.")

    if fmt_key == "csv":
        buf = io.StringIO()
        df.to_csv(buf, index=False, encoding="utf-8-sig")
        filename = f"{job_id}_voters_{sheet_key}.csv"
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_key.capitalize(), index=False)
    buf.seek(0)
    filename = f"{job_id}_voters_{sheet_key}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
