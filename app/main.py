import json
import logging
import os
import math
import re
import shutil
import time
from io import BytesIO
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, Response
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

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB", "pdf2excel")
MONGO_TIMEOUT_MS = int(os.getenv("MONGO_TIMEOUT_MS", "5000"))
AUTH_USERNAME = os.getenv("APP_AUTH_USER", "datainteg")
AUTH_PASSWORD = os.getenv("APP_AUTH_PASS", "Welcome@911")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-session-secret")

mongo_client = None
mongo_db = None
mongo_jobs = None
mongo_files = None
logger = logging.getLogger(__name__)


app = FastAPI(title="PDF to Excel OCR", version="1.0.0")
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
    try:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context=context,
            status_code=status_code,
        )
    except TypeError:
        return templates.TemplateResponse(name, context, status_code=status_code)


def mongo_enabled() -> bool:
    return mongo_jobs is not None and mongo_files is not None and Binary is not None


def init_mongo() -> None:
    global mongo_client, mongo_db, mongo_jobs, mongo_files
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
        mongo_jobs.create_index("job_id", unique=True)
        mongo_files.create_index([("job_id", 1), ("file_key", 1)], unique=True)
        print(f"MongoDB connected: {MONGO_URI} db={MONGO_DB_NAME}")
    except Exception as exc:
        print(f"MongoDB disabled: {exc}")
        mongo_client = None
        mongo_db = None
        mongo_jobs = None
        mongo_files = None


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


def dataframe_rows(df: pd.DataFrame, limit: int) -> List[Dict]:
    if df.empty:
        return []
    subset = df.head(limit).copy()
    subset = subset.where(pd.notna(subset), None)
    return subset.to_dict(orient="records")


def paged_preview_from_df(df: pd.DataFrame, page: int, page_size: int) -> Dict:
    total_rows = len(df)
    total_pages = max(1, math.ceil(total_rows / page_size)) if page_size > 0 else 1
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    part = df.iloc[start:end].copy()
    part = part.where(pd.notna(part), None)

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
        json.dumps(payload, ensure_ascii=False, indent=2),
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

    run_batch.configure_tesseract()
    run_batch.configure_tessdata_prefix()
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
    try:
        run_batch.save_excel(all_records, excel_path)
    except Exception as exc:
        artifacts["excel"]["error"] = f"Excel generation failed: {exc_message(exc)}"
    mark_artifact_from_disk(
        artifact=artifacts["excel"],
        path=excel_path,
        default_error="Excel file was not created.",
    )

    csv_write_error = None
    try:
        run_batch.save_csv_files(all_records, csv_dir)
    except Exception as exc:
        csv_write_error = f"CSV generation failed: {exc_message(exc)}"

    mark_artifact_from_disk(
        artifact=artifacts["csv_marathi"],
        path=marathi_csv_path,
        default_error=csv_write_error or "Marathi CSV file was not created.",
    )
    mark_artifact_from_disk(
        artifact=artifacts["csv_english"],
        path=english_csv_path,
        default_error=csv_write_error or "English CSV file was not created.",
    )

    preview_limit = 100
    preview_marathi = preview_block(columns=[], rows=[], truncated=False)
    preview_english = preview_block(columns=[], rows=[], truncated=False)
    try:
        marathi_df, english_df = run_batch.build_output_frames(all_records)
        preview_marathi = preview_block(
            columns=list(marathi_df.columns),
            rows=dataframe_rows(marathi_df, preview_limit),
            truncated=len(marathi_df) > preview_limit,
        )
        preview_english = preview_block(
            columns=list(english_df.columns),
            rows=dataframe_rows(english_df, preview_limit),
            truncated=len(english_df) > preview_limit,
        )
    except Exception as exc:
        warnings.append(f"Preview build failed: {exc_message(exc)}")

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
    write_job_meta(job_dir, payload)
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
            mongo_jobs.update_one({"job_id": job_id}, {"$set": payload})
        except Exception as exc:
            warnings.append(f"mongo_meta_refresh: {exc_message(exc)}")

    job_elapsed = time.time() - job_started
    print(
        f"[job {job_id}] completed total_records={len(all_records)} "
        f"status={payload['status']} elapsed={job_elapsed:.1f}s"
    )
    return payload


@app.on_event("startup")
def on_startup() -> None:
    ensure_dirs()
    ensure_required_templates()
    run_batch.configure_tesseract()
    run_batch.configure_tessdata_prefix()
    init_mongo()


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
    validate_job_id(job_id)
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
    validate_job_id(job_id)
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
    validate_job_id(job_id)
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
