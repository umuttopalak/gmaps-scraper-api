import asyncio
import csv
import dataclasses
import io
import json
import logging
import math
import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
import httpx

# ---------------------------------------------------------------------------
# UTF-8 JSON Response
# ---------------------------------------------------------------------------

class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wrapper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GOSOM_API_URL = os.environ.get("GOSOM_API_URL", "http://localhost:8080/api/v1")
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "gmapsdata/json")).resolve()
MAX_POLLS = 720          # ~36 dk (ortalama 3sn aralıkla)

# job_id doğrulama: kesin UUID formatı
_VALID_JOB_ID = re.compile(
    r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)

# Klasör adı doğrulama: YYYY-MM-DD formatı
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------------------
# Value / CSV helpers  (mevcut – değişiklik yok)
# ---------------------------------------------------------------------------

def _clean_value(v):
    """Tek bir hücreyi temizle: NaN/inf → None, JSON string → parsed obje, 'null' → None."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "null" or s == "":
        return None
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            pass
    return v


def _parse_csv(text: str) -> list[dict]:
    """CSV text → temizlenmiş JSON-uyumlu dict listesi (pandas'sız)."""
    reader = csv.DictReader(io.StringIO(text))
    results = []
    for row in reader:
        results.append({k: _clean_value(v) for k, v in row.items()})
    return results

# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def _cache_key(query: str, depth: int, max_reviews: int) -> tuple:
    return (query.strip().lower(), depth, max_reviews, date.today().isoformat())

# ---------------------------------------------------------------------------
# JobRecord
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class JobRecord:
    job_id: str
    query: str
    depth: int
    max_reviews: int
    status: str           # "pending" | "ok" | "failed"
    created_at: str       # ISO datetime
    date: str             # "YYYY-MM-DD"
    result_count: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_jobs: dict[str, JobRecord] = {}          # job_id → JobRecord
_query_index: dict[tuple, str] = {}       # cache_key → job_id
_active_tasks: set[asyncio.Task] = set()  # background poll task'ları
_creating_keys: set[tuple] = set()        # race condition koruması

# ---------------------------------------------------------------------------
# Path helpers — job'un kendi tarihini kullanır
# ---------------------------------------------------------------------------

def _validate_job_id(job_id: str) -> None:
    """job_id'nin UUID formatında olduğunu doğrula (path traversal koruması)."""
    if not _VALID_JOB_ID.match(job_id):
        raise ValueError(f"Geçersiz job_id formatı: {job_id}")


def _ensure_dir_for_date(job_date: str) -> Path:
    """Yazma işlemleri için: klasörü oluşturarak döner."""
    d = DATA_ROOT / job_date
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dir_for_date(job_date: str) -> Path:
    """Okuma işlemleri için: klasörü oluşturmadan döner."""
    return DATA_ROOT / job_date


def _index_path_for_date(job_date: str) -> Path:
    return _ensure_dir_for_date(job_date) / "index.json"


def _result_path_for_write(job_id: str, job_date: str) -> Path:
    """Yazma: klasörü oluşturur, path traversal korumalı."""
    _validate_job_id(job_id)
    p = (_ensure_dir_for_date(job_date) / f"{job_id}.json").resolve()
    root = DATA_ROOT.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"Geçersiz yol: {p}")
    return p


def _result_path_for_read(job_id: str, job_date: str) -> Path:
    """Okuma: klasör oluşturmaz, path traversal korumalı."""
    _validate_job_id(job_id)
    p = (_dir_for_date(job_date) / f"{job_id}.json").resolve()
    root = DATA_ROOT.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"Geçersiz yol: {p}")
    return p

# ---------------------------------------------------------------------------
# Disk persistence helpers
# ---------------------------------------------------------------------------

def _write_index_records(job_date: str, records: list[dict]) -> None:
    """index.json'ı atomik olarak diske yaz (senkron, thread'de çağrılır)."""
    path = _index_path_for_date(job_date)
    tmp = path.with_name("index.json.tmp")
    try:
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log.error(f"index.json yazılamadı ({job_date}): {e}")
        raise


async def _save_index(job_date: str | None = None):
    """Belirtilen günün index.json'ını asyncio thread'inde yaz.
    Snapshot event-loop thread'inde alınır, yazma ayrı thread'de yapılır."""
    target_date = job_date or date.today().isoformat()
    records = [r.to_dict() for r in _jobs.values() if r.date == target_date]
    await asyncio.to_thread(_write_index_records, target_date, records)


def _load_todays_index():
    """Startup'ta bugünün index.json'ını oku, _jobs ve _query_index'i doldur."""
    today = date.today().isoformat()
    path = _dir_for_date(today) / "index.json"
    if not path.exists():
        return
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"index.json okunamadı: {e}")
        return
    for d in records:
        try:
            rec = JobRecord.from_dict(d)
        except (TypeError, KeyError) as e:
            log.warning(f"index.json kaydı atlandı (şema uyumsuzluğu): {e}")
            continue
        _jobs[rec.job_id] = rec
        key = _cache_key(rec.query, rec.depth, rec.max_reviews)
        _query_index[key] = rec.job_id
    log.info(f"Diskten {len(_jobs)} job yüklendi (bugün)")

# ---------------------------------------------------------------------------
# Daily cleanup — dünden eski klasörleri sil (bugün+dün korunur)
# ---------------------------------------------------------------------------

_last_cleanup_date: str = ""
_cleanup_fail_count: int = 0


async def _daily_cleanup():
    global _last_cleanup_date, _cleanup_fail_count
    today = date.today().isoformat()
    if _last_cleanup_date == today:
        return
    # Yeni gün — hata sayacını sıfırla
    _cleanup_fail_count = 0
    _last_cleanup_date = today

    if not DATA_ROOT.exists():
        return

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Aktif job tarihlerini topla — bu tarihlerin klasörlerini silme
    active_dates = {r.date for r in _jobs.values() if r.status == "pending"}

    dirs_to_delete = []
    try:
        children = list(DATA_ROOT.iterdir())
        _cleanup_fail_count = 0
    except OSError as e:
        _cleanup_fail_count += 1
        if _cleanup_fail_count < 3:
            log.error(f"DATA_ROOT listelenemedi, cleanup atlandı (#{_cleanup_fail_count}/3), tekrar denenecek: {e}")
            _last_cleanup_date = ""
        else:
            log.error(f"DATA_ROOT listelenemedi (#{_cleanup_fail_count}/3), bugün artık denenmeyecek: {e}")
        return

    for child in children:
        if child.is_dir() and _DATE_DIR_RE.match(child.name) and child.name < yesterday and child.name not in active_dates:
            dirs_to_delete.append(child)

    for d in dirs_to_delete:
        log.info(f"Eski klasör siliniyor: {d}")
        try:
            await asyncio.to_thread(shutil.rmtree, d)
        except Exception as e:
            log.warning(f"Klasör silinemedi {d}: {e}")

    # Bellekteki eski kayıtları temizle (dün+bugün ve pending olanlar hariç)
    stale_ids = [
        jid for jid, r in _jobs.items()
        if r.date < yesterday and r.status != "pending"
    ]
    for jid in stale_ids:
        del _jobs[jid]
    stale_keys = [
        k for k, jid in _query_index.items()
        if k[3] < yesterday and not (_jobs.get(jid) and _jobs[jid].status == "pending")
    ]
    for k in stale_keys:
        del _query_index[k]

    if stale_ids:
        log.info(f"Bellekten {len(stale_ids)} eski job temizlendi")

# ---------------------------------------------------------------------------
# Overnight job cleanup helper
# ---------------------------------------------------------------------------

def _evict_stale_job(rec: JobRecord):
    """Gece yarısını geçmiş bitmiş job'ları bellekten temizle."""
    today = date.today().isoformat()
    if rec.date == today:
        return
    # Job artık pending değil ve bugüne ait değil → bellekten sil
    _jobs.pop(rec.job_id, None)
    # _query_index'ten de temizle (eski tarihli key)
    stale_keys = [
        k for k, jid in _query_index.items()
        if jid == rec.job_id
    ]
    for k in stale_keys:
        del _query_index[k]
    log.info(f"[{rec.job_id[:8]}] Gece yarısı geçmiş job bellekten temizlendi")

# ---------------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------------

def _write_result_sync(job_id: str, job_date: str, json_data: list[dict]) -> None:
    """JSON sonucu diske atomik olarak yaz (senkron, thread'de çağrılır)."""
    result_path = _result_path_for_write(job_id, job_date)
    tmp = result_path.parent / f"{result_path.name}.tmp"
    try:
        tmp.write_text(
            json.dumps(json_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, result_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


async def _poll_and_finalize(rec: JobRecord):
    """Gosom'u poll et, tamamlanınca CSV indir → JSON'a çevir → diske yaz."""
    poll_count = 0
    poll_interval = 1.0

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            while poll_count < MAX_POLLS:
                await asyncio.sleep(poll_interval)
                poll_count += 1

                try:
                    res = await client.get(f"{GOSOM_API_URL}/jobs/{rec.job_id}")
                    data = res.json()
                except Exception as e:
                    log.warning(f"[{rec.job_id[:8]}] Poll #{poll_count} hata: {e}")
                    poll_interval = min(poll_interval + 0.5, 3.0)
                    continue

                status = data.get("status") or data.get("Status")
                log.info(f"[{rec.job_id[:8]}] Poll #{poll_count}: status='{status}'")

                if status in ("ok", "finished", "completed", "success"):
                    # CSV indir
                    try:
                        csv_res = await client.get(
                            f"{GOSOM_API_URL}/jobs/{rec.job_id}/download",
                            timeout=120,
                        )
                    except Exception as e:
                        rec.status = "failed"
                        rec.error = f"CSV indirme hatası: {e}"
                        await _save_index(rec.date)
                        log.error(f"[{rec.job_id[:8]}] CSV indirilemedi: {e}")
                        return

                    try:
                        csv_text = csv_res.content.decode("utf-8")
                    except UnicodeDecodeError:
                        csv_text = csv_res.content.decode("utf-8", errors="replace")
                        log.warning(f"[{rec.job_id[:8]}] CSV UTF-8 decode hatası, replace ile devam")

                    if not csv_text.strip():
                        json_data: list[dict] = []
                    else:
                        json_data = _parse_csv(csv_text)

                    # JSON'ı diske yaz (thread'de, event loop bloklanmaz)
                    try:
                        await asyncio.to_thread(
                            _write_result_sync, rec.job_id, rec.date, json_data
                        )
                    except (OSError, ValueError) as e:
                        rec.status = "failed"
                        rec.error = f"Sonuç dosyası yazılamadı: {e}"
                        await _save_index(rec.date)
                        log.error(f"[{rec.job_id[:8]}] Diske yazma hatası: {e}")
                        return

                    # Önce veriyi set et, sonra status'u "ok" yap
                    rec.result_count = len(json_data)
                    rec.status = "ok"
                    await _save_index(rec.date)
                    log.info(
                        f"[{rec.job_id[:8]}] Tamamlandı: {rec.result_count} kayıt "
                        f"({poll_count} poll)"
                    )
                    # Gece yarısını geçtiyse bellekten temizle
                    _evict_stale_job(rec)
                    return

                elif status in ("failed", "error"):
                    rec.status = "failed"
                    rec.error = data.get("error") or data.get("Error") or str(data)
                    await _save_index(rec.date)
                    log.error(f"[{rec.job_id[:8]}] Gosom hata: {rec.error}")
                    _evict_stale_job(rec)
                    return

                # pending / working → devam
                poll_interval = min(poll_interval + 0.5, 3.0)

        # Max poll aşıldı
        rec.status = "failed"
        rec.error = f"Timeout: {MAX_POLLS} poll sonra tamamlanmadı"
        await _save_index(rec.date)
        log.error(f"[{rec.job_id[:8]}] Polling timeout")
        _evict_stale_job(rec)

    except asyncio.CancelledError:
        log.info(f"[{rec.job_id[:8]}] Poll task iptal edildi")
    except Exception as e:
        rec.status = "failed"
        rec.error = str(e)
        log.exception(f"[{rec.job_id[:8]}] Beklenmeyen hata: {e}")
        try:
            await _save_index(rec.date)
        except Exception as save_err:
            log.error(f"[{rec.job_id[:8]}] Index kaydedilemedi: {save_err}")
        _evict_stale_job(rec)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _load_todays_index()
    await _daily_cleanup()

    # Pending job'lar için polling yeniden başlat
    for rec in _jobs.values():
        if rec.status == "pending":
            log.info(f"Pending job için polling yeniden başlatılıyor: {rec.job_id[:8]}")
            task = asyncio.create_task(_poll_and_finalize(rec))
            _active_tasks.add(task)
            task.add_done_callback(_active_tasks.discard)

    yield

    # Shutdown — aktif task'ları cancel
    for task in _active_tasks:
        task.cancel()
    if _active_tasks:
        await asyncio.gather(*_active_tasks, return_exceptions=True)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Google Maps Custom JSON API", lifespan=lifespan)

# ---------------------------------------------------------------------------
# POST /api/jobs — Job oluştur veya cache hit dön
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(body: dict):
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query gerekli")

    # Parametre validasyonu
    try:
        depth = int(body.get("depth", 1))
        max_reviews = int(body.get("max_reviews", 10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="depth ve max_reviews tam sayı olmalı")

    if not (1 <= depth <= 10):
        raise HTTPException(status_code=400, detail="depth 1-10 arasında olmalı")
    if not (0 <= max_reviews <= 500):
        raise HTTPException(status_code=400, detail="max_reviews 0-500 arasında olmalı")

    await _daily_cleanup()

    key = _cache_key(query, depth, max_reviews)

    # Deduplication kontrolü
    if key in _query_index:
        existing = _jobs.get(_query_index[key])
        if existing:
            if existing.status == "ok":
                return UTF8JSONResponse(
                    content={
                        "job_id": existing.job_id,
                        "status": "ok",
                        "result_count": existing.result_count,
                        "download_url": f"/api/jobs/{existing.job_id}/result",
                    },
                    status_code=200,
                )
            elif existing.status == "pending":
                return UTF8JSONResponse(
                    content={
                        "job_id": existing.job_id,
                        "status": "pending",
                    },
                    status_code=200,
                )
            # status == "failed" → yeniden dene (aşağıya devam)

    # Race condition koruması: aynı key için eşzamanlı oluşturma engelle
    if key in _creating_keys:
        raise HTTPException(
            status_code=409,
            detail="Bu sorgu için job zaten oluşturuluyor, lütfen biraz bekleyin",
        )
    _creating_keys.add(key)

    try:
        # Gosom'a POST
        payload = {
            "name": f"wrapper_{datetime.now().strftime('%H%M%S')}",
            "keywords": [query],
            "lang": "tr",
            "depth": depth,
            "max_reviews": max_reviews,
            "max_time": 3600,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.post(f"{GOSOM_API_URL}/jobs", json=payload)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gosom'a bağlanılamadı: {e}")

        if res.status_code != 201:
            raise HTTPException(
                status_code=502,
                detail=f"Gosom job oluşturamadı: {res.status_code} - {res.text[:300]}",
            )

        try:
            job_id = res.json().get("id")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gosom geçersiz yanıt gövdesi: {e}")
        if not job_id:
            raise HTTPException(status_code=502, detail="Gosom job id döndürmedi")

        # job_id güvenlik kontrolü
        try:
            _validate_job_id(job_id)
        except ValueError:
            raise HTTPException(status_code=502, detail=f"Gosom geçersiz job_id döndürdü: {job_id}")

        log.info(f"Yeni job oluşturuldu: {job_id[:8]} query='{query}'")

        rec = JobRecord(
            job_id=job_id,
            query=query,
            depth=depth,
            max_reviews=max_reviews,
            status="pending",
            created_at=datetime.now().isoformat(),
            date=date.today().isoformat(),
        )

        _jobs[job_id] = rec
        _query_index[key] = job_id
        await _save_index(rec.date)

        # Background poll başlat
        task = asyncio.create_task(_poll_and_finalize(rec))
        _active_tasks.add(task)
        task.add_done_callback(_active_tasks.discard)

        return UTF8JSONResponse(
            content={"job_id": job_id, "status": "pending"},
            status_code=201,
        )
    finally:
        _creating_keys.discard(key)

# ---------------------------------------------------------------------------
# GET /api/jobs — Bugünün tüm job'larını listele
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
async def list_jobs():
    await _daily_cleanup()
    today = date.today().isoformat()
    jobs = []
    for rec in _jobs.values():
        if rec.date != today:
            continue
        item = {
            "job_id": rec.job_id,
            "query": rec.query,
            "status": rec.status,
            "created_at": rec.created_at,
        }
        if rec.status == "ok":
            item["result_count"] = rec.result_count
            item["download_url"] = f"/api/jobs/{rec.job_id}/result"
        elif rec.status == "failed":
            item["error"] = rec.error
        jobs.append(item)
    return UTF8JSONResponse(content=jobs)

# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id} — Tek job durumu
# ---------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    try:
        _validate_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz job_id formatı")

    rec = _jobs.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job bulunamadı")

    resp: dict = {
        "job_id": rec.job_id,
        "query": rec.query,
        "status": rec.status,
        "created_at": rec.created_at,
    }
    if rec.status == "ok":
        resp["result_count"] = rec.result_count
        resp["download_url"] = f"/api/jobs/{rec.job_id}/result"
    elif rec.status == "failed":
        resp["error"] = rec.error

    return UTF8JSONResponse(content=resp)

# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/result — JSON sonucu indir
# ---------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}/result")
async def get_result(job_id: str):
    try:
        _validate_job_id(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz job_id formatı")

    rec = _jobs.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job bulunamadı")

    if rec.status == "pending":
        return UTF8JSONResponse(
            content={"status": "pending", "message": "Job henüz tamamlanmadı"},
            status_code=202,
        )

    if rec.status == "failed":
        raise HTTPException(status_code=422, detail=f"Job başarısız: {rec.error}")

    result_file = _result_path_for_read(rec.job_id, rec.date)
    if not result_file.exists():
        raise HTTPException(status_code=410, detail="Sonuç dosyası silinmiş")

    return FileResponse(
        path=str(result_file),
        media_type="application/json; charset=utf-8",
        filename=f"{rec.job_id}.json",
    )
