import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from urllib.parse import urlencode, urlparse

import httpx
from filelock import FileLock

from backend.db import AppError, add_job, connect, dump, enqueue_summary, now, require_row, uid, unpack


API = "https://cloud-api.yandex.net/v1/disk"
OAUTH = "https://oauth.yandex.ru"
PUBLIC_HOSTS = {"disk.yandex.ru", "disk.yandex.com", "yadi.sk"}
EXTENSIONS = {".mp4", ".webm"}


def _oauth_error(message="Не удалось подключить Яндекс Диск"):
    return AppError("yandex_oauth_failed", message, 502)


def _api_error(response, *, link=False):
    messages = {
        401: "Доступ к Яндекс Диску истёк. Подключите его заново",
        403: "У приложения нет доступа к этому ресурсу",
        404: "Ссылка или файл больше недоступны" if link else "Папка Яндекс Диска не найдена",
        429: "Яндекс Диск временно ограничил частоту запросов",
    }
    if response.status_code == 429:
        try:
            retry_after = max(1, min(300, int(response.headers.get("retry-after", 15))))
        except (TypeError, ValueError):
            retry_after = 15
        return AppError("yandex_rate_limited", messages[429], 503,
                        details={"retry_after": retry_after})
    status = 401 if response.status_code == 401 else 404 if response.status_code == 404 else 503 if response.status_code >= 500 else 422
    code = "yandex_reconnect" if response.status_code == 401 else "yandex_unavailable"
    return AppError(code, messages.get(response.status_code, "Яндекс Диск вернул ошибку"), status)


def _request(client, method, url, **kwargs):
    for attempt in range(3):
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError):
            raise AppError("yandex_network", "Нет связи с Яндекс Диском. Проверьте интернет", 503) from None
        if response.status_code < 400:
            return response
        error = _api_error(response, link="public" in url)
        if error.payload["code"] != "yandex_rate_limited" or attempt == 2:
            raise error
        response.close()
        hinted = error.payload.get("details", {}).get("retry_after", 15)
        time.sleep(min(30, max(hinted, 2 ** attempt)))
    raise AssertionError("unreachable")


class TokenStore:
    def __init__(self, config):
        self.path = config.yandex_token_file
        self.lock = FileLock(str(self.path) + ".lock")

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def read(self):
        with self.lock:
            return self._read()

    def _write(self, value):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(value, separators=(",", ":")))
        pending.chmod(0o600)
        os.replace(pending, self.path)
        self.path.chmod(0o600)

    def write(self, value):
        with self.lock:
            self._write(value)

    def delete(self):
        with self.lock:
            self.path.unlink(missing_ok=True)


def connected(config):
    return bool(config.yandex_client_id and TokenStore(config).read())


def oauth_start(config):
    if not config.yandex_client_id:
        raise AppError("yandex_not_configured", "Укажите YANDEX_CLIENT_ID в файле .env", 409)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(minutes=10)
    with connect(config, write=True) as db:
        db.execute("DELETE FROM oauth_attempts WHERE provider='yandex' OR expires_at<=?", (now(),))
        db.execute("INSERT INTO oauth_attempts VALUES (?,?,?,?,?)",
                   (state, "yandex", verifier, created.isoformat(timespec="microseconds"),
                    expires.isoformat(timespec="microseconds")))
    query = urlencode({
        "response_type": "code", "client_id": config.yandex_client_id,
        "redirect_uri": config.yandex_redirect_uri, "scope": "cloud_api:disk.read",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return OAUTH + "/authorize?" + query


def oauth_callback(config, code, state):
    if not code or not state:
        raise _oauth_error("Яндекс не вернул код подтверждения")
    with connect(config, write=True) as db:
        row = require_row(db, "SELECT * FROM oauth_attempts WHERE state=? AND provider='yandex'",
                          (state,), "Запрос OAuth не найден")
        db.execute("DELETE FROM oauth_attempts WHERE state=?", (state,))
        if row["expires_at"] <= now():
            raise AppError("oauth_expired", "Запрос OAuth устарел. Подключите Диск заново", 409)
        verifier = row["verifier"]
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(OAUTH + "/token", data={
                "grant_type": "authorization_code", "code": code,
                "client_id": config.yandex_client_id, "redirect_uri": config.yandex_redirect_uri,
                "code_verifier": verifier,
            })
    except (httpx.TimeoutException, httpx.NetworkError):
        raise _oauth_error("Нет связи с сервером OAuth Яндекса") from None
    if response.status_code >= 400:
        raise _oauth_error()
    payload = response.json()
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise _oauth_error()
    TokenStore(config).write({
        "access_token": payload["access_token"], "refresh_token": payload["refresh_token"],
        "expires_at": time.time() + int(payload.get("expires_in", 0)),
    })
    stamp = now()
    with connect(config, write=True) as db:
        db.execute("""INSERT INTO cloud_integrations(provider,status,folder,connected_at)
                      VALUES ('yandex','connected',?,?)
                      ON CONFLICT(provider) DO UPDATE SET status='connected',folder=excluded.folder,
                      connected_at=excluded.connected_at,last_error=NULL""", (config.yandex_disk_path, stamp))


def access_token(config):
    store = TokenStore(config)
    with store.lock:
        value = store._read()
        if not value:
            raise AppError("yandex_not_connected", "Подключите Яндекс Диск", 409)
        if float(value.get("expires_at", 0)) > time.time() + 60:
            return value["access_token"]
        try:
            with httpx.Client(timeout=15) as client:
                response = client.post(OAUTH + "/token", data={
                    "grant_type": "refresh_token", "refresh_token": value.get("refresh_token", ""),
                    "client_id": config.yandex_client_id,
                })
        except (httpx.TimeoutException, httpx.NetworkError):
            raise _oauth_error("Нет связи с сервером OAuth Яндекса") from None
        if response.status_code >= 400:
            store.path.unlink(missing_ok=True)
            raise AppError("yandex_reconnect", "Доступ к Яндекс Диску истёк. Подключите его заново", 401)
        payload = response.json()
        value.update({"access_token": payload["access_token"],
                      "refresh_token": payload.get("refresh_token", value["refresh_token"]),
                      "expires_at": time.time() + int(payload.get("expires_in", 0))})
        store._write(value)
        return value["access_token"]


def validate_public_url(value):
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        parsed = None
    if not parsed or parsed.scheme != "https" or parsed.hostname not in PUBLIC_HOSTS or parsed.username or parsed.password:
        raise AppError("invalid_yandex_link", "Вставьте HTTPS-ссылку на публичный файл Яндекс Диска", 422)
    return value.strip()


def _clean_resource(resource, source_kind, source_key):
    if resource.get("type") != "file":
        raise AppError("yandex_not_file", "Ссылка должна вести на один видеофайл", 422)
    filename = "".join(char for char in PurePath(str(resource.get("name", ""))).name if char.isprintable()).strip()
    suffix = PurePath(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise AppError("unsupported_type", "Поддерживаются только MP4 и WebM", 415)
    mime_type = str(resource.get("mime_type") or "").lower()
    if mime_type and mime_type not in {"video/mp4", "video/webm", "application/octet-stream"}:
        raise AppError("unsupported_type", "Ресурс не распознан как видео MP4 или WebM", 415)
    size = int(resource.get("size") or 0)
    if size <= 0:
        raise AppError("invalid_upload", "Видеофайл пуст", 422)
    revision = resource.get("sha256") or resource.get("md5") or f"{size}:{resource.get('modified', '')}"
    return {
        "source_kind": source_kind, "source_key": source_key,
        "remote_path": resource.get("path"), "filename": filename[:240],
        "mime_type": mime_type or None, "size": size, "revision": revision,
        "remote_sha256": resource.get("sha256"), "remote_md5": resource.get("md5"),
        "modified": resource.get("modified"),
    }


def public_resource(url):
    public_key = validate_public_url(url)
    with httpx.Client(timeout=20) as client:
        response = _request(client, "GET", API + "/public/resources", params={"public_key": public_key})
    return _clean_resource(response.json(), "public", public_key)


def list_private_resources(config):
    token = access_token(config)
    headers = {"Authorization": "OAuth " + token, "Accept": "application/json"}
    items, offset, limit = [], 0, 200
    with httpx.Client(timeout=20) as client:
        while True:
            response = _request(client, "GET", API + "/resources", headers=headers, params={
                "path": config.yandex_disk_path, "limit": limit, "offset": offset,
                "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.type,_embedded.items.mime_type,"
                          "_embedded.items.size,_embedded.items.modified,_embedded.items.md5,_embedded.items.sha256",
            })
            page = response.json().get("_embedded", {}).get("items", [])
            items.extend(page)
            if len(page) < limit:
                break
            offset += len(page)
    return [_clean_resource(item, "private", item.get("path", "")) for item in items
            if item.get("type") == "file" and PurePath(str(item.get("name", ""))).suffix.lower() in EXTENSIONS]


def register_import(config, resource):
    if resource["size"] > config.max_video:
        raise AppError("too_large", f"Размер файла превышает {config.max_video // 2**20} МиБ", 413)
    stamp = now()
    with connect(config, write=True) as db:
        existing = db.execute("""SELECT * FROM cloud_imports
            WHERE provider='yandex' AND source_key=? AND revision=?""",
                              (resource["source_key"], resource["revision"])).fetchone()
        if existing:
            return dict(existing), True
        fingerprints, values = ["revision=?"], [resource["revision"]]
        if resource.get("remote_sha256"):
            fingerprints.append("remote_sha256=?")
            values.append(resource["remote_sha256"])
        if resource.get("remote_md5"):
            fingerprints.append("remote_md5=?")
            values.append(resource["remote_md5"])
        same_file = db.execute(
            f"""SELECT * FROM cloud_imports WHERE provider='yandex' AND size=?
                AND ({' OR '.join(fingerprints)})
                ORDER BY created_at,id LIMIT 1""",
            (resource["size"], *values),
        ).fetchone()
        if same_file:
            import_id = uid()
            db.execute("""INSERT INTO cloud_imports(id,provider,source_kind,source_key,remote_path,filename,
                mime_type,size,revision,remote_sha256,remote_md5,modified,status,error,meeting_id,created_at,updated_at)
                VALUES (?,'yandex',?,?,?,?,?,?,?,?,?,?,'duplicate',NULL,?,?,?)""",
                       (import_id, resource["source_kind"], resource["source_key"], resource["remote_path"],
                        resource["filename"], resource["mime_type"], resource["size"], resource["revision"],
                        resource["remote_sha256"], resource["remote_md5"], resource["modified"],
                        same_file["meeting_id"], stamp, stamp))
            row = dict(db.execute("SELECT * FROM cloud_imports WHERE id=?", (import_id,)).fetchone())
            return row, True
        import_id, meeting_id = uid(), uid()
        db.execute("""INSERT INTO cloud_imports(id,provider,source_kind,source_key,remote_path,filename,
            mime_type,size,revision,remote_sha256,remote_md5,modified,status,created_at,updated_at)
            VALUES (?,'yandex',?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                   (import_id, resource["source_kind"], resource["source_key"], resource["remote_path"],
                    resource["filename"], resource["mime_type"], resource["size"], resource["revision"],
                    resource["remote_sha256"], resource["remote_md5"], resource["modified"], stamp, stamp))
        db.execute("""INSERT INTO meetings(id,filename,created_at,updated_at,status,stage,source,cloud_import_id)
                      VALUES (?,?,?,?,'queued','queued','yandex',?)""",
                   (meeting_id, resource["filename"], stamp, stamp, import_id))
        db.execute("UPDATE cloud_imports SET meeting_id=? WHERE id=?", (meeting_id, import_id))
        job_id = add_job(db, "process_video", meeting=meeting_id, params={"cloud_import_id": import_id})
        row = dict(db.execute("SELECT * FROM cloud_imports WHERE id=?", (import_id,)).fetchone())
        row["job_id"] = job_id
        return row, False


def retry_import(config, import_id, *, require_failed=True):
    """Return a failed cloud import to the shared processing queue exactly once."""
    stamp = now()
    with connect(config, write=True) as db:
        record = require_row(db, "SELECT * FROM cloud_imports WHERE id=?", (import_id,), "Импорт не найден")
        if record["status"] != "error":
            if require_failed:
                raise AppError("import_not_failed", "Повторить можно только неудавшийся импорт", 409)
            return {"import_id": import_id, "meeting_id": record["meeting_id"],
                    "job_id": None, "status": record["status"]}
        meeting = require_row(db, "SELECT * FROM meetings WHERE id=?", (record["meeting_id"],),
                              "Встреча не найдена")
        active = db.execute("""SELECT id FROM jobs WHERE meeting_id=? AND state IN ('queued','running')
                             AND kind IN ('process_video','summarize') ORDER BY created_at DESC LIMIT 1""",
                            (meeting["id"],)).fetchone()
        if active:
            job_id = active["id"]
        elif meeting["transcript_revision"]:
            job_id = enqueue_summary(db, meeting["id"], meeting["transcript_revision"])
        else:
            job_id = add_job(db, "process_video", meeting=meeting["id"],
                             params={"cloud_import_id": import_id})
        if meeting["transcript_revision"]:
            db.execute("UPDATE meetings SET status='processing',error=NULL,updated_at=? WHERE id=?",
                       (stamp, meeting["id"]))
        else:
            db.execute("""UPDATE meetings SET status='queued',stage='queued',error=NULL,updated_at=?
                          WHERE id=?""", (stamp, meeting["id"]))
        db.execute("UPDATE cloud_imports SET status='queued',error=NULL,updated_at=? WHERE id=?",
                   (stamp, import_id))
    return {"import_id": import_id, "meeting_id": record["meeting_id"],
            "job_id": job_id, "status": "queued"}


def _is_transient_import_error(record):
    error = unpack(record.get("error"), {}) or {}
    code = error.get("code")
    message = str(error.get("message", "")).lower()
    return code in {"yandex_rate_limited", "yandex_network"} or "частоту запросов" in message


def _retry_transient_imports(config):
    """Retry private and public imports with bounded exponential backoff."""
    with connect(config) as db:
        rows = db.execute("""SELECT i.*,
            (SELECT count(*) FROM jobs j WHERE j.meeting_id=i.meeting_id
             AND j.kind='process_video' AND j.state='failed') AS failure_count
            FROM cloud_imports i WHERE i.provider='yandex' AND i.status='error'""").fetchall()
        records = [dict(row) for row in rows]
    current = datetime.now(timezone.utc)
    for record in records:
        if not _is_transient_import_error(record):
            continue
        failures = max(1, int(record.get("failure_count") or 1))
        delay = min(300, 15 * (2 ** min(failures, 4)))
        try:
            updated = datetime.fromisoformat(record["updated_at"])
        except (TypeError, ValueError):
            updated = current - timedelta(seconds=delay)
        if updated + timedelta(seconds=delay) <= current:
            retry_import(config, record["id"], require_failed=False)


def scan(config):
    stamp = now()
    with connect(config, write=True) as db:
        db.execute("""INSERT INTO cloud_integrations(provider,status,folder,last_check_at)
                      VALUES ('yandex','connected',?,?)
                      ON CONFLICT(provider) DO UPDATE SET last_check_at=excluded.last_check_at,folder=excluded.folder""",
                   (config.yandex_disk_path, stamp))
    try:
        resources = list_private_resources(config)
        for resource in resources:
            register_import(config, resource)
        # Also recover imports created through a public link, not only folder entries.
        _retry_transient_imports(config)
    except AppError as error:
        if error.payload["code"] == "yandex_reconnect":
            TokenStore(config).delete()
        with connect(config, write=True) as db:
            db.execute("UPDATE cloud_integrations SET last_error=? WHERE provider='yandex'", (dump(error.payload),))
        raise
    completed = now()
    with connect(config, write=True) as db:
        db.execute("""UPDATE cloud_integrations SET status='connected',last_success_at=?,last_error=NULL
                      WHERE provider='yandex'""", (completed,))
    return {"checked_at": stamp, "found": len(resources)}


def _download_link(config, record):
    params = ({"public_key": record["source_key"]} if record["source_kind"] == "public"
              else {"path": record["remote_path"]})
    endpoint = "/public/resources/download" if record["source_kind"] == "public" else "/resources/download"
    headers = {"Accept": "application/json"}
    if record["source_kind"] == "private":
        headers["Authorization"] = "OAuth " + access_token(config)
    try:
        with httpx.Client(timeout=20) as client:
            response = _request(client, "GET", API + endpoint, headers=headers, params=params)
    except AppError as error:
        if record["source_kind"] == "private" and error.payload["code"] == "yandex_reconnect":
            TokenStore(config).delete()
        raise
    href = response.json().get("href")
    if not href or urlparse(href).scheme != "https":
        raise AppError("yandex_download_failed", "Яндекс Диск не вернул безопасную ссылку скачивания", 502)
    return href


def download_import(config, import_id, destination):
    with connect(config) as db:
        record = require_row(db, "SELECT * FROM cloud_imports WHERE id=?", (import_id,), "Импорт не найден")
        record = dict(record)
    href = _download_link(config, record)
    for attempt in range(3):
        size = 0
        sha256, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
        destination.unlink(missing_ok=True)
        try:
            with httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True) as client:
                with client.stream("GET", href, headers={"Accept": "application/octet-stream"}) as response:
                    if response.status_code >= 400:
                        error = _api_error(response, link=record["source_kind"] == "public")
                        if error.payload["code"] == "yandex_rate_limited" and attempt < 2:
                            hinted = error.payload.get("details", {}).get("retry_after", 15)
                            time.sleep(min(30, max(hinted, 2 ** attempt)))
                            continue
                        raise error
                    declared = int(response.headers.get("content-length", 0) or 0)
                    if declared > config.max_video:
                        raise AppError("too_large", f"Размер файла превышает {config.max_video // 2**20} МиБ", 413)
                    with destination.open("xb") as target:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > config.max_video:
                                raise AppError("too_large", f"Размер файла превышает {config.max_video // 2**20} МиБ", 413)
                            target.write(chunk)
                            sha256.update(chunk)
                            md5.update(chunk)
            break
        except (httpx.TimeoutException, httpx.NetworkError):
            destination.unlink(missing_ok=True)
            if attempt == 2:
                raise AppError("yandex_network", "Скачивание из Яндекс Диска прервалось", 503) from None
            time.sleep(2 ** attempt)
    if not size:
        raise AppError("invalid_upload", "Скачанный файл пуст")
    if record["remote_sha256"] and sha256.hexdigest() != record["remote_sha256"]:
        raise AppError("cloud_file_changed", "Файл изменился во время скачивания. Повторите импорт", 409)
    if record["remote_md5"] and md5.hexdigest() != record["remote_md5"]:
        raise AppError("cloud_file_changed", "Файл изменился во время скачивания. Повторите импорт", 409)
    return {"bytes": size, "sha256": sha256.hexdigest()}


def disconnect(config):
    TokenStore(config).delete()
    with connect(config, write=True) as db:
        db.execute("""INSERT INTO cloud_integrations(provider,status,folder)
                      VALUES ('yandex','disconnected',?)
                      ON CONFLICT(provider) DO UPDATE SET status='disconnected',last_error=NULL""",
                   (config.yandex_disk_path,))
