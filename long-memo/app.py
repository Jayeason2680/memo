"""Single-user HTTPS service for the long Memo beta."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from pipeline import DATA, folder, named_report, process, read_job, save_job, update, write_exports, transcript_rows

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
pool = ThreadPoolExecutor(max_workers=1)
running: set[str] = set()
running_lock = threading.Lock()
login_failures: dict[str, list[float]] = {}
CHUNK_LIMIT = 4 * 1024 * 1024
MAX_FILE = 1024 * 1024 * 1024
VALID_EXT = {".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".wav", ".webm"}
DOWNLOADS = {"transcript.txt", "comprehensive.docx", "brief.docx", "memo-downloads.zip", "bilingual-transcript.txt", "bilingual-transcript.docx"}


@app.middleware("http")
async def private_responses(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def config() -> tuple[str, str]:
    password = os.environ.get("MEMO_PASSWORD", "")
    secret = os.environ.get("MEMO_SESSION_SECRET", "")
    if len(password) < 12 or len(secret) < 32:
        raise RuntimeError("Set MEMO_PASSWORD (12+ characters) and MEMO_SESSION_SECRET (32+ characters)")
    return password, secret


def signature(message: str) -> str:
    return hmac.new(config()[1].encode(), message.encode(), hashlib.sha256).hexdigest()


def session(request: Request, *, mutation=False) -> str:
    token = request.cookies.get("memo_session", "")
    try:
        expiry, nonce, mac = token.split(".")
        if int(expiry) < time.time() or not hmac.compare_digest(mac, signature(expiry + "." + nonce)):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(401, "Sign in to continue")
    if mutation:
        origin = request.headers.get("origin", "")
        host = str(request.base_url).rstrip("/")
        valid_origins = {"https://" + request.headers.get("host", ""), "http://" + request.headers.get("host", "")}
        if origin and origin not in valid_origins:
            raise HTTPException(403, "Origin mismatch")
        csrf = request.headers.get("x-memo-csrf", "")
        if not hmac.compare_digest(csrf, signature("csrf." + nonce)):
            raise HTTPException(403, "Session check failed")
    return nonce


def public(job: dict, *, include_report=True) -> dict:
    keys = ("id", "title", "filename", "size", "offset", "mode", "status", "error", "duration_seconds", "total_parts", "current_part", "speaker_names", "report", "created", "fingerprint", "translation_part", "translation_total", "include_translation")
    result = {key: job.get(key) for key in keys}
    if not include_report:
        result.pop("report", None)
    else:
        result["report"] = named_report(job)
        result["transcript"] = transcript_rows(job)
        result["has_translation"] = bool(job.get("translation_batches"))
    speakers = {segment["speaker"] for part in job.get("transcribed", []) for segment in part["segments"]}
    result["speakers"] = sorted(speakers)
    return result


def dispatch(job_id: str) -> None:
    with running_lock:
        if job_id in running:
            return
        running.add(job_id)
    def task():
        try:
            process(job_id)
        finally:
            with running_lock:
                running.discard(job_id)
    pool.submit(task)


@app.on_event("startup")
def startup() -> None:
    config()
    for path in DATA.glob("*/job.json"):
        try:
            job = read_job(path.parent.name)
            if job["status"] in ("queued", "preparing", "transcribing", "translating", "writing"):
                dispatch(job["id"])
        except (ValueError, KeyError, OSError):
            continue


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (Path(__file__).parent / "index.html").read_text()


@app.get("/assets/{name}")
def assets(name: str):
    if name not in ("studio.css", "studio.js", "icon.svg", "manifest.webmanifest"):
        raise HTTPException(404, "File not found")
    return FileResponse(Path(__file__).parent / name)


@app.get("/health")
def health():
    if not DATA.is_dir() or not os.access(DATA, os.W_OK):
        raise HTTPException(503, "Recording storage is unavailable")
    return {"status": "ok"}


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    address = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [stamp for stamp in login_failures.get(address, []) if now - stamp < 900]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many attempts. Try again later")
    if not hmac.compare_digest(body.password, config()[0]):
        login_failures[address] = recent + [now]
        raise HTTPException(401, "Incorrect password")
    login_failures.pop(address, None)
    nonce = secrets.token_hex(16)
    expiry = str(int(time.time() + 7 * 86400))
    local_http = request.url.scheme == "http" and request.url.hostname in ("127.0.0.1", "localhost", "::1")
    response.set_cookie("memo_session", f"{expiry}.{nonce}.{signature(expiry + '.' + nonce)}",
                        httponly=True, secure=not local_http, samesite="strict", max_age=7 * 86400)
    return {"csrf": signature("csrf." + nonce)}


@app.get("/api/session")
def get_session(request: Request):
    return {"csrf": signature("csrf." + session(request))}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    session(request, mutation=True)
    response.delete_cookie("memo_session")
    return {"signed_out": True}


class NewJob(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=MAX_FILE)
    mode: str
    terms: str = Field(default="", max_length=500)
    fingerprint: str = Field(default="", pattern=r"^([a-f0-9]{64})?$")
    include_translation: bool = True


@app.post("/api/jobs")
def new_job(body: NewJob, request: Request):
    session(request, mutation=True)
    if body.mode not in ("personal", "meeting") or Path(body.filename).suffix.lower() not in VALID_EXT:
        raise HTTPException(400, "Choose a supported audio file and mode")
    job_id = uuid.uuid4().hex
    path = folder(job_id)
    path.mkdir(mode=0o700)
    job = {"id": job_id, "title": body.title.strip(), "filename": Path(body.filename).name,
           "size": body.size, "offset": 0, "mode": body.mode, "terms": body.terms.strip(),
           "created": int(time.time()), "status": "uploading", "error": None,
           "transcribed": [], "speaker_names": {}, "speaker_refs": [],
           "fingerprint": body.fingerprint, "include_translation": body.include_translation,
           "translation_batches": []}
    save_job(job)
    return public(job)


@app.get("/api/jobs")
def list_jobs(request: Request):
    session(request)
    jobs = []
    for path in DATA.glob("*/job.json"):
        try:
            jobs.append(public(read_job(path.parent.name), include_report=False))
        except (ValueError, KeyError, OSError):
            continue
    return sorted(jobs, key=lambda job: job["created"], reverse=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    session(request)
    try:
        return public(read_job(job_id))
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Job not found")


@app.put("/api/jobs/{job_id}/upload")
async def upload(job_id: str, request: Request):
    session(request, mutation=True)
    try:
        offset = int(request.headers.get("x-upload-offset", "-1"))
        payload = bytearray()
        async for piece in request.stream():
            payload.extend(piece)
            if len(payload) > CHUNK_LIMIT:
                raise HTTPException(413, "Upload chunk too large")
        with running_lock:
            job = read_job(job_id)
            if job["status"] != "uploading":
                raise HTTPException(409, "Upload is closed")
            if offset != job["offset"]:
                raise HTTPException(409, f"Resume from byte {job['offset']}")
            if not payload or offset + len(payload) > job["size"]:
                raise HTTPException(400, "Invalid upload chunk")
            original = folder(job_id) / "original"
            stored_size = original.stat().st_size if original.exists() else 0
            if stored_size < offset:
                raise HTTPException(409, "Stored audio is incomplete. Import the recording again.")
            # A crash after writing bytes but before saving the offset is recoverable.
            if stored_size > offset:
                with original.open("r+b") as stream:
                    stream.truncate(offset)
            with original.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            job["offset"] += len(payload)
            save_job(job)
            return {"offset": job["offset"]}
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Job not found")


@app.post("/api/jobs/{job_id}/finish")
def finish(job_id: str, request: Request):
    session(request, mutation=True)
    try:
        with running_lock:
            job = read_job(job_id)
            original = folder(job_id) / "original"
            if (job["status"] != "uploading" or job["offset"] != job["size"]
                    or not original.is_file() or original.stat().st_size != job["size"]):
                raise HTTPException(409, "Upload is incomplete")
            update(job_id, status="queued")
        dispatch(job_id)
        return {"status": "queued"}
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Job not found")


@app.post("/api/jobs/{job_id}/speaker-reference")
async def speaker_reference(job_id: str, request: Request):
    session(request, mutation=True)
    job = read_job(job_id)
    if job["status"] != "uploading" or job["mode"] != "meeting" or len(job["speaker_refs"]) >= 4:
        raise HTTPException(409, "Up to four speaker clips can be added before processing")
    name = unquote(request.headers.get("x-speaker-name", "")).strip()
    ext = Path(request.headers.get("x-file-name", "")).suffix.lower()
    if not name or len(name) > 80 or ext not in VALID_EXT:
        raise HTTPException(400, "Provide a speaker name and supported audio clip")
    content = await request.body()
    if not content or len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "Speaker clip must be under 5 MB")
    clip = folder(job_id) / f"speaker-{len(job['speaker_refs'])}{ext}"
    clip.write_bytes(content)
    from pipeline import duration
    try:
        seconds = duration(clip)
        if not 2 <= seconds <= 10:
            raise ValueError("Speaker clips must be 2–10 seconds long")
    except Exception as exc:
        clip.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)[:160])
    job["speaker_refs"].append({"name": name, "file": clip.name})
    save_job(job)
    return {"count": len(job["speaker_refs"])}


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str, request: Request):
    session(request, mutation=True)
    job = read_job(job_id)
    if job["status"] != "failed" or job["offset"] != job["size"]:
        raise HTTPException(409, "This job cannot be retried")
    update(job_id, status="queued", error=None)
    dispatch(job_id)
    return {"status": "queued"}


class SpeakerNames(BaseModel):
    names: dict[str, str]


@app.post("/api/jobs/{job_id}/speakers")
def speakers(job_id: str, body: SpeakerNames, request: Request):
    session(request, mutation=True)
    job = read_job(job_id)
    if job["status"] != "ready":
        raise HTTPException(409, "Wait for the transcript")
    known = {segment["speaker"] for part in job["transcribed"] for segment in part["segments"]}
    if set(body.names) - known or any(len(name) > 80 for name in body.names.values()):
        raise HTTPException(400, "Invalid speaker name")
    job["speaker_names"] = {key: value.strip() for key, value in body.names.items() if value.strip()}
    save_job(job)
    write_exports(job)
    return public(job)


@app.get("/api/jobs/{job_id}/downloads/{name}")
def download(job_id: str, name: str, request: Request):
    session(request)
    if name not in DOWNLOADS:
        raise HTTPException(404, "File not found")
    job = read_job(job_id)
    if job["status"] != "ready":
        raise HTTPException(409, "Downloads are not ready")
    path = folder(job_id) / name
    if not path.is_file():
        raise HTTPException(404, "This download is not available for this recording")
    title = re.sub(r'[^\w\s-]', '', job["title"]).strip()[:60] or "Memo"
    return FileResponse(path, filename=title + " - " + name)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    session(request, mutation=True)
    job = read_job(job_id)
    if job["status"] not in ("uploading", "ready", "failed"):
        raise HTTPException(409, "Wait for processing to finish before deleting")
    shutil.rmtree(folder(job_id))
    return {"deleted": True}
