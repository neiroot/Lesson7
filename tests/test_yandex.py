from urllib.parse import parse_qs, urlparse
import stat

import pytest

from backend import yandex_disk
from backend.db import AppError, connect, migrate


def resource(*, revision="hash-1", source_kind="public", source_key="https://disk.yandex.ru/d/example"):
    return {
        "source_kind": source_kind, "source_key": source_key,
        "remote_path": "disk:/Видеовстречи/встреча.mp4" if source_kind == "private" else None,
        "filename": "встреча.mp4", "mime_type": "video/mp4", "size": 100,
        "revision": revision, "remote_sha256": None, "remote_md5": None, "modified": "2026-11-11T10:00:00Z",
    }


def test_public_url_is_restricted_to_yandex_https():
    assert yandex_disk.validate_public_url("https://disk.yandex.ru/d/example").endswith("/example")
    for value in ("http://disk.yandex.ru/d/example", "https://example.org/video.mp4", "not-a-url"):
        with pytest.raises(AppError) as error:
            yandex_disk.validate_public_url(value)
        assert error.value.payload["code"] == "invalid_yandex_link"


def test_cloud_revision_is_idempotent_and_changed_file_is_new(env):
    _, config, _ = env
    first, duplicate = yandex_disk.register_import(config, resource())
    same, duplicate_again = yandex_disk.register_import(config, resource())
    changed, changed_duplicate = yandex_disk.register_import(config, resource(revision="hash-2"))
    assert not duplicate and duplicate_again and not changed_duplicate
    assert first["meeting_id"] == same["meeting_id"]
    assert changed["meeting_id"] != first["meeting_id"]
    with connect(config) as db:
        assert db.execute("SELECT count(*) FROM cloud_imports").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='process_video'").fetchone()[0] == 2


def test_same_file_from_link_and_folder_is_not_processed_twice(env):
    _, config, _ = env
    linked = resource(source_kind="public", source_key="https://disk.yandex.ru/d/example")
    linked["remote_sha256"] = linked["revision"]
    linked["remote_md5"] = "md5-1"
    first, duplicate = yandex_disk.register_import(config, linked)
    folder = resource(source_kind="private", source_key="disk:/Видеовстречи/встреча.mp4")
    folder["remote_sha256"] = linked["remote_sha256"]
    folder["remote_md5"] = linked["remote_md5"]
    second, folder_duplicate = yandex_disk.register_import(config, folder)
    repeated, repeated_duplicate = yandex_disk.register_import(config, folder)

    assert not duplicate and folder_duplicate and repeated_duplicate
    assert second["status"] == "duplicate"
    assert repeated["id"] == second["id"]
    assert second["meeting_id"] == first["meeting_id"]
    with connect(config) as db:
        assert db.execute("SELECT count(*) FROM meetings").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='process_video'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM cloud_imports").fetchone()[0] == 2


def test_cloud_download_uses_existing_analysis_pipeline(env, monkeypatch):
    client, config, worker = env
    record, _ = yandex_disk.register_import(config, resource())

    def download(_, import_id, destination):
        assert import_id == record["id"]
        destination.write_bytes(b"media")
        return {"bytes": 5, "sha256": "fake"}

    monkeypatch.setattr(yandex_disk, "download_import", download)
    assert worker.run_one()
    assert worker.run_one()
    meeting = client.get(f"/api/meetings/{record['meeting_id']}").json()
    assert meeting["status"] == "ready" and meeting["source"] == "yandex"
    imports = client.get("/api/cloud-imports").json()
    assert imports[0]["status"] == "ready" and imports[0]["meeting_id"] == meeting["id"]


def test_scan_records_time_and_does_not_duplicate(env, monkeypatch):
    _, config, _ = env
    private = resource(source_kind="private", source_key="disk:/Видеовстречи/встреча.mp4")
    monkeypatch.setattr(yandex_disk, "list_private_resources", lambda _: [private])
    assert yandex_disk.scan(config)["found"] == 1
    assert yandex_disk.scan(config)["found"] == 1
    with connect(config) as db:
        assert db.execute("SELECT count(*) FROM cloud_imports").fetchone()[0] == 1
        state = db.execute("SELECT * FROM cloud_integrations WHERE provider='yandex'").fetchone()
        assert state["last_check_at"] and state["last_success_at"] and state["last_error"] is None


def test_rate_limit_is_retried_with_server_delay(monkeypatch):
    class Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"retry-after": "2"}

        def close(self):
            pass

    class Client:
        def __init__(self):
            self.responses = [Response(429), Response(429), Response(200)]

        def request(self, *_args, **_kwargs):
            return self.responses.pop(0)

    delays = []
    monkeypatch.setattr(yandex_disk.time, "sleep", delays.append)
    response = yandex_disk._request(Client(), "GET", yandex_disk.API + "/resources")
    assert response.status_code == 200
    assert delays == [2, 2]


def test_scan_requeues_import_failed_by_legacy_rate_limit(env, monkeypatch):
    _, config, _ = env
    private = resource(source_kind="private", source_key="disk:/Видеовстречи/встреча.mp4")
    record, _ = yandex_disk.register_import(config, private)
    legacy_error = '{"code":"yandex_unavailable","message":"Яндекс Диск временно ограничил частоту запросов"}'
    with connect(config, write=True) as db:
        db.execute("UPDATE jobs SET state='failed' WHERE meeting_id=?", (record["meeting_id"],))
        db.execute("UPDATE meetings SET status='error',error=? WHERE id=?",
                   (legacy_error, record["meeting_id"]))
        db.execute("UPDATE cloud_imports SET status='error',error=?,updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                   (legacy_error, record["id"]))
    monkeypatch.setattr(yandex_disk, "list_private_resources", lambda _: [private])

    yandex_disk.scan(config)

    with connect(config) as db:
        imported = db.execute("SELECT * FROM cloud_imports WHERE id=?", (record["id"],)).fetchone()
        meeting = db.execute("SELECT * FROM meetings WHERE id=?", (record["meeting_id"],)).fetchone()
        queued = db.execute("SELECT count(*) FROM jobs WHERE meeting_id=? AND state='queued'",
                            (record["meeting_id"],)).fetchone()[0]
        assert imported["status"] == "queued" and imported["error"] is None
        assert meeting["status"] == "queued" and meeting["error"] is None
        assert queued == 1


def test_oauth_start_uses_pkce_without_client_secret(tmp_path):
    from backend.config import Config

    config = Config(data=tmp_path / "data", models=tmp_path / "models", calibration=tmp_path / "cal.json",
                    yandex_client_id="client-id")
    migrate(config)
    url = yandex_disk.oauth_start(config)
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["client-id"]
    assert query["scope"] == ["cloud_api:disk.read"]
    assert query["code_challenge_method"] == ["S256"]
    assert "client_secret" not in query
    with connect(config) as db:
        attempt = db.execute("SELECT * FROM oauth_attempts WHERE state=?", (query["state"][0],)).fetchone()
        assert attempt and attempt["verifier"] not in url
    assert not config.yandex_token_file.exists()


def test_expired_token_is_refreshed_and_kept_private(tmp_path, monkeypatch):
    from backend.config import Config

    config = Config(data=tmp_path / "data", models=tmp_path / "models", calibration=tmp_path / "cal.json",
                    yandex_client_id="client-id")
    migrate(config)
    store = yandex_disk.TokenStore(config)
    store.write({"access_token": "old", "refresh_token": "refresh", "expires_at": 0})

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}

    class Client:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def post(self, *_args, **_kwargs): return Response()

    monkeypatch.setattr(yandex_disk.httpx, "Client", Client)
    assert yandex_disk.access_token(config) == "new"
    assert store.read()["refresh_token"] == "new-refresh"
    assert stat.S_IMODE(config.yandex_token_file.stat().st_mode) == 0o600


def test_public_link_endpoint_returns_existing_meeting(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(yandex_disk, "public_resource", lambda _: resource())
    first = client.post("/api/cloud-imports/yandex-link", json={"url": "https://disk.yandex.ru/d/example"})
    second = client.post("/api/cloud-imports/yandex-link", json={"url": "https://disk.yandex.ru/d/example"})
    assert first.status_code == 202 and second.status_code == 202
    assert not first.json()["duplicate"] and second.json()["duplicate"]
    assert first.json()["meeting_id"] == second.json()["meeting_id"]
