import ipaddress
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request as FastAPIRequest
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, validator


DATA_DIR = Path(os.getenv("DATA_DIR", "/data/jobs"))
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(250 * 1024 * 1024)))
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "60"))
ALLOWED_STORAGE_HOSTS = {
    value.strip().lower()
    for value in os.getenv("ALLOWED_STORAGE_HOSTS", "").split(",")
    if value.strip()
}
ALLOWED_STORAGE_PORTS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_STORAGE_PORTS", "80,443,8080,8443").split(",")
    if value.strip()
}
ALLOWED_CLIENT_IPS = {
    ipaddress.ip_network(value.strip(), strict=False)
    for value in os.getenv("ALLOWED_CLIENT_IPS", "").split(",")
    if value.strip()
}
API_KEY = os.getenv("API_KEY", "")

app = FastAPI(title="PDF Compression API", version="0.1.0")
jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Redirect non consentito", headers, fp)


class CompressionRequest(BaseModel):
    source_url: str = Field(..., description="URL HTTP(S) dello storage locale raggiungibile dal server")
    compression: str = Field("balanced", description="screen, ebook, printer, prepress o balanced")
    filename: Optional[str] = Field(None, description="Nome del PDF risultante")
    source_headers: Dict[str, str] = Field(default_factory=dict)

    @validator("source_url")
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source_url deve essere un URL HTTP o HTTPS valido")
        return value

    @validator("compression")
    def validate_compression(cls, value: str) -> str:
        if value not in {"screen", "ebook", "printer", "prepress", "balanced"}:
            raise ValueError("profilo di compressione non supportato")
        return value


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_job(job_id: str, **updates) -> None:
    with jobs_lock:
        jobs[job_id].update(updates, updated_at=now())


def client_ip(request: FastAPIRequest) -> str:
    # X-Forwarded-For viene usato solo se il deploy dichiara esplicitamente un reverse proxy fidato.
    if os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def authenticate(request: FastAPIRequest, x_api_key: Optional[str]) -> None:
    remote_ip = client_ip(request)
    if ALLOWED_CLIENT_IPS:
        try:
            parsed_ip = ipaddress.ip_address(remote_ip)
        except ValueError:
            raise HTTPException(status_code=403, detail="Client IP non valida")
        if not any(parsed_ip in network for network in ALLOWED_CLIENT_IPS):
            raise HTTPException(status_code=403, detail="Client IP non autorizzata")
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key non valida")


def validate_storage_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not ALLOWED_STORAGE_HOSTS:
        raise HTTPException(status_code=503, detail="ALLOWED_STORAGE_HOSTS non configurato")
    if host not in ALLOWED_STORAGE_HOSTS:
        raise HTTPException(status_code=403, detail="Host di storage non autorizzato")
    if port not in ALLOWED_STORAGE_PORTS:
        raise HTTPException(status_code=403, detail="Porta di storage non autorizzata")


def download_source(source_url: str, destination: Path, headers: Dict[str, str]) -> None:
    validate_storage_url(source_url)
    safe_headers = {
        key: value for key, value in headers.items()
        if key.lower() in {"authorization", "x-storage-token", "x-api-key"}
    }
    request = Request(source_url, headers=safe_headers, method="GET")
    total = 0
    # Niente redirect automatici: un redirect potrebbe aggirare l'allowlist dell'host.
    opener = build_opener(NoRedirectHandler())
    with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError("Il file supera MAX_DOWNLOAD_BYTES")
            output.write(chunk)


def compress_pdf(input_path: Path, output_path: Path, profile: str) -> None:
    # Ghostscript non riceve input dell'utente nella shell: lista di argomenti separata.
    profile = "/ebook" if profile == "balanced" else f"/{profile}"
    command = [
        os.getenv("GHOSTSCRIPT_BIN", "gs"), "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET",
        "-sDEVICE=pdfwrite", f"-dPDFSETTINGS={profile}",
        f"-sOutputFile={output_path}", str(input_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:] or "Ghostscript ha restituito un errore")


def process_job(job_id: str, payload: CompressionRequest) -> None:
    job_dir = DATA_DIR / job_id
    input_path = job_dir / "input.pdf"
    output_path = job_dir / "compressed.pdf"
    try:
        job_dir.mkdir(parents=True, exist_ok=False)
        set_job(job_id, status="downloading")
        download_source(payload.source_url, input_path, payload.source_headers)
        set_job(job_id, status="compressing", input_bytes=input_path.stat().st_size)
        compress_pdf(input_path, output_path, payload.compression)
        set_job(job_id, status="completed", output_bytes=output_path.stat().st_size,
                download_url=f"/v1/compressions/{job_id}/download")
    except Exception as exc:
        set_job(job_id, status="failed", error=str(exc))
        shutil.rmtree(job_dir, ignore_errors=True)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/v1/compressions", status_code=202)
def create_compression(
    payload: CompressionRequest,
    request: FastAPIRequest,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(None),
):
    authenticate(request, x_api_key)
    validate_storage_url(payload.source_url)
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "status": "queued", "created_at": now(), "updated_at": now()}
    background_tasks.add_task(process_job, job_id, payload)
    return {"id": job_id, "status": "queued", "status_url": f"/v1/compressions/{job_id}"}


@app.get("/v1/compressions/{job_id}")
def get_compression(job_id: str, request: FastAPIRequest, x_api_key: Optional[str] = Header(None)):
    authenticate(request, x_api_key)
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return job


@app.get("/v1/compressions/{job_id}/download")
def download_compression(job_id: str, request: FastAPIRequest, x_api_key: Optional[str] = Header(None)):
    authenticate(request, x_api_key)
    with jobs_lock:
        job = jobs.get(job_id)
    output_path = DATA_DIR / job_id / "compressed.pdf"
    if not job or job.get("status") != "completed" or not output_path.exists():
        raise HTTPException(status_code=409, detail="Il file non è ancora disponibile")
    return FileResponse(output_path, media_type="application/pdf", filename=f"{job_id}.pdf")
