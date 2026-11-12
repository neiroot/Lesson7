import asyncio
import os
import re
import shutil
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from backend import voices, yandex_disk
from backend.assets import check_ollama
from backend.config import CONSENT_TEXT, CONSENT_VERSION, ROOT, Config, model_lock
from backend.db import AppError, add_job, connect, defer_unlink, dump, enqueue_summary, migrate, now, require_row, uid, unpack
from backend.media import receive_file
from backend.presentation import meeting_row, safe_job, transcript


def problem(code, message, status):
    return JSONResponse({"code": code, "message": message}, status_code=status)


def serialized_meeting(row):
    return {key: row[key] for key in (
        "id", "filename", "created_at", "updated_at", "duration_ms", "status", "stage", "source",
    )}


class YandexLink(BaseModel):
    url: str


def serialized_draft(row, config):
    quality = unpack(row["quality"], {})
    expired = row["expires_at"] <= now()
    return {**{key: row[key] for key in ("id", "created_at", "expires_at", "speech_ms")},
            "status": "expired" if expired else row["status"], "reasons": quality.get("reasons", []),
            "audio_available": bool(not expired and row["path"] and (config.data / row["path"]).is_file())}


def audio_response(path, requested_range):
    try:
        file = path.open("rb")
    except FileNotFoundError:
        raise AppError("audio_gone", "Аудиофрагмент больше недоступен", 410) from None
    size = os.fstat(file.fileno()).st_size
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
    start, end, status = 0, size - 1, 200
    if requested_range is not None:
        match = re.fullmatch(r"bytes=(\d{0,18})-(\d{0,18})", requested_range)
        if match and (match[1] or match[2]):
            if match[1]:
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
        if not match or not (match[1] or match[2]) or start > end:
            file.close()
            return JSONResponse({"code": "invalid_range", "message": "Диапазон аудио недоступен"},
                                status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        status = 206
    headers["Content-Length"] = str(end - start + 1)

    def read():
        with file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(read(), status_code=status, media_type="audio/wav", headers=headers)


def create_app(config=None):
    config = config or Config()

    async def yandex_loop():
        rate_limit_delay = config.yandex_poll_seconds
        while True:
            if yandex_disk.connected(config):
                try:
                    await asyncio.to_thread(yandex_disk.scan, config)
                    rate_limit_delay = config.yandex_poll_seconds
                except AppError as error:
                    if error.payload["code"] == "yandex_rate_limited":
                        hinted = error.payload.get("details", {}).get("retry_after", 15)
                        rate_limit_delay = min(300, max(hinted, rate_limit_delay * 2))
                except Exception:
                    # A transient programming/runtime failure must not silently kill future scans.
                    with suppress(Exception):
                        with connect(config, write=True) as db:
                            db.execute("""UPDATE cloud_integrations SET last_error=?
                                          WHERE provider='yandex'""",
                                       (dump({"code": "scan_failed",
                                              "message": "Фоновая проверка не завершилась"}),))
            await asyncio.sleep(rate_limit_delay)

    @asynccontextmanager
    async def lifespan(_):
        migrate(config)
        task = asyncio.create_task(yandex_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request, call_next):
        if request.headers.get("host", "").lower() not in config.hosts:
            return problem("host_denied", "Допустим только локальный адрес приложения", 403)
        if request.method not in {"GET", "HEAD"} and request.url.path.startswith("/api"):
            allowed = {"http://" + host for host in config.hosts}
            if request.headers.get("origin") not in allowed:
                return problem("origin_denied", "Откройте приложение с разрешённого локального адреса", 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        if not config.development:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "media-src 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(AppError)
    async def app_error(_, error):
        return JSONResponse(error.payload, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, __):
        return problem("invalid_request", "Проверьте поля запроса", 422)

    @app.exception_handler(sqlite3.Error)
    async def database_error(_, __):
        return problem("database_failed", "Локальное хранилище временно недоступно", 503)

    @app.exception_handler(OSError)
    async def file_error(_, error):
        if error.errno == 28:
            return problem("disk_full", "На диске закончилось свободное место", 507)
        return problem("storage_failed", "Не удалось прочитать или сохранить локальный файл", 503)

    @app.exception_handler(ClientDisconnect)
    async def disconnected(_, __):
        return problem("upload_interrupted", "Загрузка прервалась", 400)

    # Only one request can stream a new meeting at a time. SQLite also guards the queued slot.
    upload_lock = asyncio.Lock()

    def available_slot(db):
        active = db.execute("SELECT id,filename FROM meetings WHERE status IN ('queued','processing') LIMIT 1").fetchone()
        if active:
            raise AppError("video_busy", "Дождитесь обработки текущей встречи", 409,
                           details={"meeting_id": active["id"], "filename": active["filename"]})

    @app.post("/api/meetings", status_code=202)
    async def upload_meeting(request: Request):
        if upload_lock.locked():
            raise AppError("upload_busy", "Уже загружается другая встреча", 409)
        async with upload_lock:
            with connect(config) as db:
                available_slot(db)
            meeting_id = uid()
            pending = config.data / f"incoming/{meeting_id}.upload"
            final = pending.with_suffix(".bin")
            committed = False
            try:
                filename = await receive_file(request, pending, config.max_video, {".mp4", ".webm"})
                with connect(config, write=True) as db:
                    available_slot(db)
                    pending.replace(final)
                    stamp = now()
                    db.execute("""INSERT INTO meetings(id,filename,created_at,updated_at,status)
                                  VALUES (?,?,?,?,'queued')""", (meeting_id, filename, stamp, stamp))
                    job_id = add_job(db, "process_video", meeting=meeting_id)
                committed = True
                return {"meeting_id": meeting_id, "job_id": job_id, "status": "queued"}
            finally:
                pending.unlink(missing_ok=True)
                if not committed:
                    final.unlink(missing_ok=True)

    @app.get("/api/meetings")
    def list_meetings():
        with connect(config) as db:
            return [{
                **serialized_meeting(row), "needs_review_count": row["needs_review_count"],
                "summary_status": row["summary_status"],
            } for row in db.execute(
                """SELECT m.*,(SELECT COUNT(*) FROM meeting_speakers s WHERE s.meeting_id=m.id
                   AND s.status='needs_review') AS needs_review_count FROM meetings m ORDER BY created_at DESC,id"""
            )]

    @app.get("/api/integrations/yandex")
    def yandex_status():
        with connect(config) as db:
            row = db.execute("SELECT * FROM cloud_integrations WHERE provider='yandex'").fetchone()
        error = unpack(row["last_error"]) if row and row["last_error"] else None
        return {
            "configured": bool(config.yandex_client_id), "connected": yandex_disk.connected(config),
            "folder": config.yandex_disk_path, "poll_seconds": config.yandex_poll_seconds,
            "connected_at": row["connected_at"] if row else None,
            "last_check_at": row["last_check_at"] if row else None,
            "last_success_at": row["last_success_at"] if row else None, "last_error": error,
        }

    @app.post("/api/integrations/yandex/oauth/start")
    def start_yandex_oauth():
        return {"authorize_url": yandex_disk.oauth_start(config)}

    @app.get("/api/integrations/yandex/callback")
    async def yandex_callback(code: str | None = None, state: str | None = None,
                              error: str | None = None):
        if error:
            return RedirectResponse("/?yandex=denied", status_code=303)
        try:
            await asyncio.to_thread(yandex_disk.oauth_callback, config, code, state)
            try:
                await asyncio.to_thread(yandex_disk.scan, config)
            except AppError:
                pass
        except AppError:
            return RedirectResponse("/?yandex=error", status_code=303)
        return RedirectResponse("/?yandex=connected", status_code=303)

    @app.post("/api/integrations/yandex/check")
    async def check_yandex():
        return await asyncio.to_thread(yandex_disk.scan, config)

    @app.delete("/api/integrations/yandex", status_code=204)
    def disconnect_yandex():
        yandex_disk.disconnect(config)
        return Response(status_code=204)

    @app.post("/api/cloud-imports/yandex-link", status_code=202)
    async def import_yandex_link(payload: YandexLink):
        resource = await asyncio.to_thread(yandex_disk.public_resource, payload.url)
        record, duplicate = yandex_disk.register_import(config, resource)
        return {"import_id": record["id"], "meeting_id": record["meeting_id"],
                "status": record["status"], "duplicate": duplicate}

    @app.get("/api/cloud-imports")
    def list_cloud_imports():
        with connect(config) as db:
            rows = db.execute("""SELECT i.*,m.status AS meeting_status,m.stage AS meeting_stage
                FROM cloud_imports i LEFT JOIN meetings m ON m.id=i.meeting_id
                ORDER BY i.created_at DESC,i.id""").fetchall()
        return [{**{key: row[key] for key in (
            "id", "source_kind", "filename", "size", "status", "meeting_id", "created_at", "updated_at",
            "meeting_status", "meeting_stage",
        )}, "error": unpack(row["error"])} for row in rows]

    @app.post("/api/cloud-imports/{import_id}/retry", status_code=202)
    def retry_cloud_import(import_id: str):
        return yandex_disk.retry_import(config, import_id)

    @app.get("/api/meetings/{meeting_id}")
    def get_meeting(meeting_id: str):
        with connect(config) as db:
            row = meeting_row(db, meeting_id)
            return {
                **serialized_meeting(row),
                "transcript": transcript(db, meeting_id),
                "transcript_revision": row["transcript_revision"], "summary_revision": row["summary_revision"],
                "summary_status": row["summary_status"], "error": unpack(row["error"]),
                "summary_error": unpack(row["summary_error"]), "model_versions": unpack(row["model_versions"], {}),
                "summary": (unpack(row["summary"]) if row["summary_revision"] == row["transcript_revision"] else None),
                "needs_review_count": db.execute("""SELECT COUNT(*) FROM meeting_speakers
                    WHERE meeting_id=? AND status='needs_review'""", (meeting_id,)).fetchone()[0],
            }

    @app.post("/api/meetings/{meeting_id}/summary/retry", status_code=202)
    def retry_summary(meeting_id: str, regenerate: bool = False):
        with connect(config, write=True) as db:
            row = meeting_row(db, meeting_id)
            if not row["transcript_revision"]:
                raise AppError("transcript_pending", "Расшифровка пока недоступна", 409)
            if row["summary_status"] == "current" and not regenerate:
                completed = db.execute("""SELECT id FROM jobs WHERE kind='summarize' AND meeting_id=?
                    AND expected_version=? AND state='succeeded' ORDER BY updated_at DESC LIMIT 1""",
                                       (meeting_id, row["transcript_revision"])).fetchone()
                if completed:
                    return {"job_id": completed["id"], "status": "succeeded"}
            job = enqueue_summary(db, meeting_id, row["transcript_revision"])
            return {"job_id": job, "status": db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0]}

    @app.delete("/api/meetings/{meeting_id}", status_code=204)
    def delete_meeting(meeting_id: str):
        with connect(config, write=True) as db:
            meeting_row(db, meeting_id)
            if db.execute("SELECT 1 FROM jobs WHERE meeting_id=? AND state IN ('queued','running')",
                          (meeting_id,)).fetchone():
                raise AppError("meeting_busy", "Дождитесь окончания обработки встречи", 409)
            for clip in db.execute("SELECT path FROM review_clips WHERE meeting_id=?", (meeting_id,)):
                defer_unlink(db, clip["path"])
            db.execute("DELETE FROM cloud_imports WHERE meeting_id=?", (meeting_id,))
            db.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        return Response(status_code=204)

    @app.get("/api/meetings/{meeting_id}/speakers")
    def get_speakers(meeting_id: str):
        with connect(config) as db:
            meeting_row(db, meeting_id)
            segments = {segment["id"]: segment for segment in transcript(db, meeting_id)}
            profiles = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM speaker_profiles")}
            result = []
            for row in db.execute("SELECT * FROM meeting_speakers WHERE meeting_id=? ORDER BY number", (meeting_id,)):
                clips = []
                for clip in db.execute("SELECT * FROM review_clips WHERE speaker_id=? ORDER BY start_ms,id", (row["id"],)):
                    exists = bool(clip["available"] and clip["path"] and (config.data / clip["path"]).is_file())
                    clips.append({
                        **{key: clip[key] for key in ("id", "start_ms", "end_ms")},
                        "eligible": bool(clip["eligible"] and exists), "audio_available": exists,
                        "quality": unpack(clip["quality"]),
                        "segments": [{**segments[item["id"]], "partial": item["partial"]}
                                     for item in unpack(clip["segments"]) if item["id"] in segments],
                    })
                job = db.execute("""SELECT * FROM jobs WHERE speaker_id=? AND kind='enroll_voice'
                    AND expected_version=? ORDER BY created_at DESC LIMIT 1""", (row["id"], row["version"])).fetchone()
                result.append({
                    **{key: row[key] for key in ("id", "number", "status", "reason", "name", "source", "profile_id", "version")},
                    "label": f"Спикер {row['number']}", "clips": clips, "job": safe_job(job) if job else None,
                    "suggested_profiles": voices.suggested_profiles(row, profiles),
                })
            return result

    @app.get("/api/meetings/{meeting_id}/clips/{clip_id}/audio")
    def clip_audio(meeting_id: str, clip_id: str, range_header: str | None = Header(None, alias="Range")):
        with connect(config) as db:
            clip = require_row(db, "SELECT * FROM review_clips WHERE meeting_id=? AND id=?",
                               (meeting_id, clip_id), "Аудиофрагмент не найден")
            if not clip["available"] or not clip["path"]:
                raise AppError("audio_gone", "Аудиофрагмент больше недоступен", 410)
            return audio_response(config.data / clip["path"], range_header)

    @app.post("/api/meetings/{meeting_id}/speakers/{speaker_id}/resolve")
    def resolve_speaker(meeting_id: str, speaker_id: str, payload: voices.Resolution,
                        key: str | None = Header(None, alias="Idempotency-Key")):
        with connect(config, write=True) as db:
            status, result = voices.idempotent(
                db, key, ["resolve", meeting_id, speaker_id], payload,
                lambda: voices.resolve(config, db, meeting_id, speaker_id, payload),
            )
        return JSONResponse(result, status_code=status)

    @app.post("/api/voice-sample-drafts", status_code=202)
    async def upload_draft(request: Request):
        draft_id = uid()
        pending = config.data / f"incoming/{draft_id}.upload"
        final = pending.with_suffix(".bin")
        committed = False
        try:
            await receive_file(request, pending, config.max_sample, {".wav", ".mp3", ".m4a"})
            with connect(config, write=True) as db:
                pending.replace(final)
                expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="microseconds")
                db.execute("""INSERT INTO voice_sample_drafts(id,status,created_at,expires_at)
                              VALUES (?,'checking',?,?)""", (draft_id, now(), expires))
                job = add_job(db, "check_sample", draft=draft_id)
            committed = True
            return {"draft_id": draft_id, "job_id": job, "status": "checking"}
        finally:
            pending.unlink(missing_ok=True)
            if not committed:
                final.unlink(missing_ok=True)

    @app.get("/api/voice-sample-drafts/{draft_id}")
    def get_draft(draft_id: str):
        with connect(config) as db:
            row = require_row(db, "SELECT * FROM voice_sample_drafts WHERE id=?", (draft_id,), "Образец не найден")
            return serialized_draft(row, config)

    @app.get("/api/voice-sample-drafts/{draft_id}/audio")
    def draft_audio(draft_id: str, range_header: str | None = Header(None, alias="Range")):
        with connect(config) as db:
            row = require_row(db, "SELECT * FROM voice_sample_drafts WHERE id=?", (draft_id,), "Образец не найден")
            if not serialized_draft(row, config)["audio_available"]:
                raise AppError("audio_gone", "Аудиообразец больше недоступен", 410)
            return audio_response(config.data / row["path"], range_header)

    @app.delete("/api/voice-sample-drafts/{draft_id}", status_code=204)
    def delete_draft(draft_id: str):
        with connect(config, write=True) as db:
            row = require_row(db, "SELECT * FROM voice_sample_drafts WHERE id=?", (draft_id,), "Образец не найден")
            voices.expire_draft(db, row)
            defer_unlink(db, f"incoming/{draft_id}.bin")
        return Response(status_code=204)

    @app.get("/api/voice-profiles")
    def list_profiles():
        with connect(config) as db:
            return [dict(row) for row in db.execute(
                """SELECT p.*,(SELECT COUNT(*) FROM speaker_embeddings e WHERE e.profile_id=p.id) AS sample_count
                   FROM speaker_profiles p ORDER BY p.created_at DESC,p.id"""
            )]

    def submit_enrollment(profile_id, payload, key):
        with connect(config, write=True) as db:
            status, result = voices.idempotent(
                db, key, ["enroll", profile_id], payload,
                lambda: voices.queue_enrollment(config, db, profile_id, payload),
            )
        return JSONResponse(result, status_code=status)

    @app.post("/api/voice-profiles", status_code=202)
    def create_profile(payload: voices.Enrollment, key: str | None = Header(None, alias="Idempotency-Key")):
        return submit_enrollment(None, payload, key)

    @app.post("/api/voice-profiles/{profile_id}/samples", status_code=202)
    def add_samples(profile_id: str, payload: voices.Enrollment,
                    key: str | None = Header(None, alias="Idempotency-Key")):
        return submit_enrollment(profile_id, payload, key)

    @app.delete("/api/voice-profiles/{profile_id}", status_code=204)
    def delete_profile(profile_id: str):
        with connect(config, write=True) as db:
            voices.profile_row(db, profile_id)
            db.execute("UPDATE meeting_speakers SET version=version+1 WHERE profile_id=?", (profile_id,))
            db.execute("DELETE FROM speaker_profiles WHERE id=?", (profile_id,))
        return Response(status_code=204)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        with connect(config) as db:
            return safe_job(require_row(db, "SELECT * FROM jobs WHERE id=?", (job_id,), "Задача не найдена"))

    @app.get("/api/health")
    def health():
        try:
            with connect(config) as db:
                db.execute("SELECT 1").fetchone()
            storage = os.access(config.data, os.W_OK)
        except sqlite3.Error:
            storage = False
        models = model_lock()
        try:
            ready = all((config.models / item["folder"] / path).is_file()
                        for item in models.values() if "folder" in item for path in item["files"])
        except OSError:
            ready = False
        ollama, digest_match = True, True
        try:
            check_ollama(timeout=1)
        except AppError as error:
            ollama = error.payload["code"] != "ollama_unavailable"
            digest_match = False
        return {"storage": storage, "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                "weights": ready, "runtimes": all(config.runtime(stage).is_file() for stage in ("diarize", "asr")),
                "ollama": ollama, "ollama_model": digest_match,
                "consent_text": CONSENT_TEXT, "consent_text_version": CONSENT_VERSION}

    dist = ROOT / "frontend/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def page():
        index = dist / "index.html"
        if not index.is_file():
            return problem("frontend_missing", "Соберите интерфейс командой npm --prefix frontend run build", 503)
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    return app


app = create_app()
