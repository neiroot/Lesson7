from fastapi.testclient import TestClient

from backend.api import create_app
from backend.config import VOICE_DIM
from backend.db import AppError, connect
from tests.conftest import write_audio


def upload(client):
    response = client.post("/api/meetings", files={"file": ("встреча.mp4", b"media", "video/mp4")})
    assert response.status_code == 202, response.text
    return response.json()


def prepared(env):
    client, _, worker = env
    meeting = upload(client)["meeting_id"]
    assert worker.run_one()
    assert worker.run_one()
    speakers = client.get(f"/api/meetings/{meeting}/speakers").json()
    assert len(speakers) == 2
    return meeting, speakers


def resolve(client, meeting, speaker, key="resolve-1", **options):
    payload = {"action": "assign_name", "name": "Анна", "confirm_name": True,
               "expected_version": speaker["version"], **options}
    return client.post(f"/api/meetings/{meeting}/speakers/{speaker['id']}/resolve", json=payload,
                       headers={"Idempotency-Key": key})


def profile_draft(env):
    client, _, worker = env
    worker.speech.diarization = {"regular": [("A", 0, 30000)], "exclusive": [("A", 0, 30000)]}
    response = client.post("/api/voice-sample-drafts", files={"file": ("голос.wav", b"audio", "audio/wav")})
    assert response.status_code == 202, response.text
    draft = response.json()["draft_id"]
    assert worker.run_one()
    assert client.get(f"/api/voice-sample-drafts/{draft}").json()["status"] == "eligible"
    return draft


def consent():
    return {"consent_confirmed": True, "consent_text_version": "1"}


def enroll(client, draft, *, profile=None, key="profile-1"):
    path = f"/api/voice-profiles/{profile}/samples" if profile else "/api/voice-profiles"
    payload = {"draft_ids": [draft], "confirm_name": True, **consent()}
    if not profile:
        payload["name"] = "Анна"
    return client.post(path, json=payload, headers={"Idempotency-Key": key})


def test_streamed_upload_limits_and_local_origin(env):
    client, config, worker = env
    assert client.get("/api/meetings", headers={"Host": "example.org"}).status_code == 403
    assert client.post("/api/meetings", headers={"Origin": "http://example.org"}).status_code == 403
    assert client.post("/api/meetings", files={"file": ("foo.txt", b"fake")}).status_code == 415
    assert client.post("/api/meetings", files={"file": ("foo.mp4", b"x" * 1025)}).status_code == 413
    assert not list((config.data / "incoming").iterdir())
    uploaded = upload(client)
    assert client.get(f"/api/jobs/{uploaded['job_id']}").json()["state"] == "queued"
    assert client.post("/api/meetings", files={"file": ("second.webm", b"data")}).status_code == 409
    worker.run_one()
    assert client.delete(f"/api/meetings/{uploaded['meeting_id']}").status_code == 409
    assert client.post("/api/meetings", files={"file": ("second.webm", b"data")}).status_code == 409


def test_review_revision_range_restart_and_skip(env):
    client, config, worker = env
    meeting, speakers = prepared(env)
    path = f"/api/meetings/{meeting}"
    before = client.get(path).json()
    assert before["summary"]["overview"] == "Обсуждение отчёта"
    assert before["status"] == "ready"
    overlap = next(row for row in before["transcript"] if row["overlap"])
    assert overlap["speaker_id"] is None
    clip = speakers[0]["clips"][0]
    clip_path = f"{path}/clips/{clip['id']}/audio"
    response = client.get(clip_path, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b"RIFF"
    assert client.get(clip_path, headers={"Range": "bytes=10000000-"}).status_code == 416
    assert client.get(f"/api/meetings/other/clips/{clip['id']}/audio").status_code == 404

    worker.recover()
    with TestClient(create_app(config), base_url="http://127.0.0.1:8000") as restored:
        assert restored.get(clip_path).status_code == 200
        assert restored.get(path).json()["transcript"] == before["transcript"]

    rejected = resolve(client, meeting, speakers[0], save_voice=True, clip_ids=[clip["id"]])
    assert rejected.status_code == 422
    assigned = resolve(client, meeting, speakers[0])
    assert assigned.status_code == 200, assigned.text
    assert resolve(client, meeting, speakers[0]).json() == assigned.json()
    assert resolve(client, meeting, speakers[0], name="Иван").status_code == 409
    assert resolve(client, meeting, speakers[0], "stale", name="Иван").status_code == 409
    changed = client.get(path).json()
    assert changed["transcript_revision"] == 2 and changed["summary"] is None
    assert [(r["id"], r["text"], r["start_ms"]) for r in changed["transcript"]] == [
        (r["id"], r["text"], r["start_ms"]) for r in before["transcript"]
    ]
    assert next(r for r in changed["transcript"] if r["speaker_id"] == speakers[0]["id"])["speaker_name"] == "Анна"
    assert client.get(clip_path).status_code == 410
    with connect(config) as db:
        for table in ("speaker_profiles", "voice_consents", "speaker_embeddings"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    worker.run_one()
    assert not (config.data / f"clips/{clip['id']}.wav").exists()
    skip = resolve(client, meeting, speakers[1], "skip", action="skip", name=None, confirm_name=False)
    assert skip.status_code == 200
    assert client.get(path).json()["transcript_revision"] == 2
    assert client.delete(path).status_code == 204
    worker.cleanup()
    assert not list((config.data / "clips").iterdir())


def test_suggested_voice_requires_manual_confirmation(env):
    client, config, worker = env
    draft = profile_draft(env)
    assert enroll(client, draft).status_code == 202
    assert worker.run_one()
    profile = client.get("/api/voice-profiles").json()[0]
    worker.speech.diarization = {
        "regular": [("A", 0, 18000), ("B", 18000, 30000)],
        "exclusive": [("A", 0, 18000), ("B", 18000, 30000)],
    }
    original_run = worker.speech.run

    def score(operation, **payload):
        if operation == "identify":
            return {"A": {profile["id"]: 0.83}, "B": {profile["id"]: 0.2}}
        return original_run(operation, **payload)

    worker.speech.run = score
    meeting, speakers = prepared(env)
    speaker, other = speakers
    assert speaker["status"] == "needs_review"
    assert speaker["name"] is None
    assert speaker["suggested_profiles"] == [{"id": profile["id"], "name": "Анна"}]
    assert other["suggested_profiles"] == []

    assignment = {"action": "assign_profile", "name": None, "profile_id": profile["id"]}
    assert resolve(client, meeting, speaker, "declined", confirm_name=False, **assignment).status_code == 422
    assert resolve(client, meeting, speaker, "confirmed", **assignment).status_code == 200
    path = f"/api/meetings/{meeting}"
    confirmed = client.get(path + "/speakers").json()[0]
    assert confirmed["status"] == "confirmed" and confirmed["name"] == "Анна"
    assert confirmed["suggested_profiles"] == []
    assert client.get("/api/voice-profiles").json()[0]["sample_count"] == profile["sample_count"]
    transcript = client.get(path).json()["transcript"]
    assert any(row["speaker_id"] == speaker["id"] and row["speaker_name"] == "Анна" for row in transcript)


def test_late_summary_cannot_overwrite_two_edits_and_retry(env):
    client, _, worker = env
    meeting, speakers = prepared(env)
    path = f"/api/meetings/{meeting}"
    assert resolve(client, meeting, speakers[0]).status_code == 200

    def during_generation():
        worker.ollama.hook = None
        for index, name in enumerate(("Вера", "Ирина")):
            speaker = client.get(path + "/speakers").json()[0]
            assert resolve(client, meeting, speaker, f"edit-{index}", name=name).status_code == 200

    worker.ollama.hook = during_generation
    worker.run_one()
    assert client.get(path).json()["summary"] is None
    worker.run_one()

    def unavailable():
        raise AppError("ollama_unavailable", "Ollama временно недоступен")

    worker.ollama.hook = unavailable
    worker.run_one()
    current = client.get(path).json()
    assert current["transcript_revision"] == 4
    assert current["status"] == "ready" and current["summary_status"] == "error"
    assert current["transcript"][0]["speaker_name"] == "Ирина"
    retry = client.post(path + "/summary/retry").json()
    assert client.post(path + "/summary/retry").json() == retry
    worker.ollama.hook = None
    worker.run_one()
    assert client.get(path).json()["summary"]["overview"] == "Ирина"


def test_create_append_delete_voice_and_deleted_target_during_embedding(env):
    client, config, worker = env
    draft = profile_draft(env)
    pending = enroll(client, draft)
    assert pending.status_code == 202, pending.text
    assert enroll(client, draft).json() == pending.json()
    worker.run_one()
    profiles = client.get("/api/voice-profiles").json()
    assert len(profiles) == 1 and profiles[0]["sample_count"] == 1
    pid = profiles[0]["id"]
    assert client.get(f"/api/voice-sample-drafts/{draft}/audio").status_code == 410
    second = profile_draft(env)
    assert enroll(client, second, profile=pid, key="append").status_code == 202
    worker.run_one()
    assert client.get("/api/voice-profiles").json()[0]["sample_count"] == 2
    with connect(config) as db:
        consent_rows = db.execute("""SELECT e.dimension,length(e.vector),c.text_version FROM speaker_embeddings e
                                  JOIN voice_consents c ON c.id=e.consent_id""").fetchall()
        assert [tuple(row) for row in consent_rows] == [(VOICE_DIM, VOICE_DIM * 4, "1")] * 2
    third = profile_draft(env)
    job = enroll(client, third, profile=pid, key="racing-delete").json()["job_id"]

    def delete_target(_):
        assert client.delete(f"/api/voice-profiles/{pid}").status_code == 204
        return [[1.0] * VOICE_DIM]

    worker.speech.embedding_hook = delete_target
    worker.run_one()
    assert client.get(f"/api/jobs/{job}").json()["state"] == "failed"
    assert client.get(f"/api/voice-sample-drafts/{third}").json()["status"] == "eligible"
    with connect(config) as db:
        for table in ("speaker_profiles", "voice_consents", "speaker_embeddings"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_clip_enrollment_is_atomic_and_requires_consent(env):
    client, config, worker = env
    meeting, speakers = prepared(env)
    clip = speakers[0]["clips"][0]
    worker.speech.embedding_hook = lambda _: [[0.0] * VOICE_DIM]
    options = {"save_voice": True, "clip_ids": [clip["id"]], **consent()}
    response = resolve(client, meeting, speakers[0], **options)
    assert response.status_code == 202, response.text
    assert resolve(client, meeting, speakers[0], "duplicate", **options).status_code == 409
    worker.run_one()
    state = client.get(f"/api/meetings/{meeting}/speakers").json()[0]
    assert state["job"]["state"] == "failed" and state["name"] is None and state["version"] == 0
    assert client.get("/api/voice-profiles").json() == []
    worker.speech.embedding_hook = None
    response = resolve(client, meeting, state, "retry", **options)
    assert response.status_code == 202, response.text
    worker.run_one()
    profile = client.get("/api/voice-profiles").json()[0]
    speakers = client.get(f"/api/meetings/{meeting}/speakers").json()
    assert speakers[0]["profile_id"] == profile["id"]
    response = resolve(client, meeting, speakers[1], "shared",
                       action="assign_profile", profile_id=profile["id"], name=None)
    assert response.status_code == 422 and response.json()["code"] == "shared_profile"
    assert resolve(client, meeting, speakers[1], "shared-confirmed", action="assign_profile",
                   profile_id=profile["id"], name=None, confirm_shared_profile=True).status_code == 200
    assert client.get("/api/voice-profiles").json()[0]["sample_count"] == 1
    assert client.delete(f"/api/voice-profiles/{profile['id']}").status_code == 204
    assert client.get(f"/api/meetings/{meeting}/speakers").json()[0]["name"] == "Анна"
    assert not (config.data / f"clips/{clip['id']}.wav").exists()


def test_restart_marks_interrupted_video_and_resumes_summary(env):
    client, config, worker = env
    first = upload(client)
    worker.claim()
    worker.recover()
    assert client.get(f"/api/meetings/{first['meeting_id']}").json()["error"]["code"] == "interrupted"
    assert not list((config.data / "incoming").iterdir())
    second = upload(client)
    worker.run_one()
    summary = worker.claim()
    assert summary["kind"] == "summarize"
    worker.recover()
    assert client.get(f"/api/jobs/{summary['id']}").json()["state"] == "queued"
    worker.run_one()
    assert client.get(f"/api/meetings/{second['meeting_id']}").json()["status"] == "ready"


def test_draft_two_speakers_rejected_and_expired(env):
    client, config, worker = env
    response = client.post("/api/voice-sample-drafts", files={"file": ("голос.wav", b"audio")})
    assert response.status_code == 202
    draft = response.json()["draft_id"]
    worker.run_one()
    result = client.get(f"/api/voice-sample-drafts/{draft}").json()
    assert result["status"] == "rejected" and result["audio_available"]
    assert enroll(client, draft).status_code == 422
    with connect(config, write=True) as db:
        db.execute("UPDATE voice_sample_drafts SET expires_at='2000-01-01' WHERE id=?", (draft,))
    worker.cleanup()
    assert client.get(f"/api/voice-sample-drafts/{draft}/audio").status_code == 410
    assert not (config.data / f"drafts/{draft}.wav").exists()


def test_silence_is_rejected_in_draft(env, monkeypatch):
    client, _, worker = env
    monkeypatch.setattr("backend.media.extract", lambda path, output, *_: write_audio(output, level=0))
    result = client.post("/api/voice-sample-drafts", files={"file": ("голос.wav", b"audio")}).json()
    worker.run_one()
    assert client.get(f"/api/voice-sample-drafts/{result['draft_id']}").json()["status"] == "rejected"
