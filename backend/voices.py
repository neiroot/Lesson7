import hashlib
import math
import struct

from pydantic import BaseModel, ConfigDict, Field

from backend.config import CONSENT_VERSION, PREPROCESS, VOICE_DIM, VOICE_MODEL, model_lock
from backend.db import AppError, add_job, defer_unlink, dump, enqueue_summary, now, require_row, uid, unpack


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm_name: bool = False
    consent_confirmed: bool = False
    consent_text_version: str | None = None
    name: str | None = Field(default=None, max_length=100)

    def confirmed_name(self):
        name = (self.name or "").strip()
        if not name or any(not char.isprintable() for char in name):
            raise AppError("invalid_name", "Введите имя длиной до 100 символов")
        return name

    def check_name(self):
        if not self.confirm_name:
            raise AppError("confirm_name", "Подтвердите имя участника")

    def check_consent(self):
        if not self.consent_confirmed or self.consent_text_version != CONSENT_VERSION:
            raise AppError("confirm_consent", "Подтвердите согласие на сохранение голосового отпечатка")


class Enrollment(Mutation):
    draft_ids: list[str] = Field(min_length=1, max_length=12)


class Resolution(Mutation):
    action: str
    expected_version: int = Field(ge=0)
    profile_id: str | None = None
    save_voice: bool = False
    clip_ids: list[str] = Field(default_factory=list, max_length=3)
    confirm_shared_profile: bool = False

    def check(self):
        if self.action == "assign_name" and self.name is not None and self.profile_id is None:
            self.check_name()
            self.confirmed_name()
        elif self.action == "assign_profile" and self.profile_id and self.name is None:
            self.check_name()
        elif not (self.action in {"skip", "reset"} and self.name is None and self.profile_id is None
                  and not self.save_voice):
            raise AppError("invalid_resolution", "Выберите одно действие и одно имя или профиль")
        if self.save_voice:
            self.check_consent()
            distinct(self.clip_ids)
        elif self.clip_ids or self.consent_confirmed or self.consent_text_version is not None:
            raise AppError("unexpected_consent", "Для записи образцов явно выберите сохранение голоса")


def distinct(ids):
    if not ids or len(ids) != len(set(ids)):
        raise AppError("sample_selection", "Выберите разные пригодные образцы")


def idempotent(db, key, scope, payload, action):
    if not key or len(key) > 128 or any(not 33 <= ord(char) <= 126 for char in key):
        raise AppError("invalid_key", "Для операции нужен корректный Idempotency-Key")
    fingerprint = hashlib.sha256(dump([scope, payload.model_dump(exclude_unset=True)]).encode()).hexdigest()
    old = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
    if old:
        if old["fingerprint"] != fingerprint:
            raise AppError("idempotency_conflict", "Этот ключ уже использован для другой операции", 409)
        return old["status"], unpack(old["response"])
    status, result = action()
    db.execute("INSERT INTO idempotency VALUES (?,?,?,?)", (key, fingerprint, status, dump(result)))
    return status, result


def speaker_row(db, meeting, speaker):
    return require_row(db, "SELECT * FROM meeting_speakers WHERE meeting_id=? AND id=?",
                       (meeting, speaker), "Спикер не найден")


# This is a display threshold for manual review, never an auto-assignment threshold.
SUGGESTION_THRESHOLD = 0.55
SUGGESTION_MARGIN = 0.10


def suggested_profiles(speaker, profiles):
    if speaker["status"] != "needs_review" or speaker["version"] != 0:
        return []
    scores = unpack(speaker["scores"], {})
    ranked = sorted(
        ((score, profile_id) for profile_id, score in scores.items()
         if profile_id in profiles and SUGGESTION_THRESHOLD <= score <= 1),
        reverse=True,
    )
    if not ranked:
        return []
    minimum = ranked[0][0] - SUGGESTION_MARGIN
    return [{"id": profile_id, "name": profiles[profile_id]}
            for score, profile_id in ranked[:3] if score >= minimum]


def profile_row(db, profile):
    return require_row(db, "SELECT * FROM speaker_profiles WHERE id=?", (profile,), "Голосовой профиль не найден")


def assignment_target(db, speaker, payload):
    if payload.action == "assign_profile":
        profile = profile_row(db, payload.profile_id)
        peers = db.execute(
            "SELECT number FROM meeting_speakers WHERE meeting_id=? AND profile_id=? AND id<>?",
            (speaker["meeting_id"], profile["id"], speaker["id"]),
        ).fetchall()
        if peers and not payload.confirm_shared_profile:
            numbers = [f"Спикер {row['number']}" for row in peers]
            raise AppError("shared_profile", "Подтвердите использование одного профиля для нескольких спикеров", 422,
                           details={"other_speakers": numbers})
        return profile["id"], profile["name"]
    if payload.action == "assign_name":
        return None, payload.confirmed_name()
    return None, None


def verify_speaker(db, speaker, expected, *, active_job=None):
    if speaker["version"] != expected:
        raise AppError("version_conflict", "Карточка изменилась. Откройте её заново", 409)
    meeting_id = speaker["meeting_id"]
    status = db.execute("SELECT transcript_revision FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if not status["transcript_revision"]:
        raise AppError("transcript_pending", "Дождитесь расшифровки встречи", 409)
    if db.execute(
        """SELECT 1 FROM jobs WHERE speaker_id=? AND kind='enroll_voice'
           AND state IN ('queued','running') AND id<>?""", (speaker["id"], active_job or ""),
    ).fetchone():
        raise AppError("speaker_busy", "Сохранение голоса для этой карточки уже выполняется", 409)


def chosen_clips(config, db, speaker, ids):
    rows = []
    for clip_id in ids:
        row = db.execute(
            "SELECT * FROM review_clips WHERE id=? AND meeting_id=? AND speaker_id=?",
            (clip_id, speaker["meeting_id"], speaker["id"]),
        ).fetchone()
        if (not row or not row["eligible"] or not row["available"] or not row["path"]
                or not (config.data / row["path"]).is_file()):
            raise AppError("clip_ineligible", "Выбранный фрагмент недоступен или непригоден")
        rows.append(row)
    return rows


def chosen_drafts(config, db, ids, *, active_job=None):
    distinct(ids)
    for job in db.execute("SELECT id,params FROM jobs WHERE kind='enroll_voice' AND state IN ('queued','running')"):
        if job["id"] != active_job and set(ids).intersection(unpack(job["params"]).get("draft_ids", [])):
            raise AppError("draft_busy", "Один из образцов уже обрабатывается", 409)
    rows = []
    for draft_id in ids:
        row = db.execute("SELECT * FROM voice_sample_drafts WHERE id=?", (draft_id,)).fetchone()
        if (not row or row["status"] != "eligible" or row["expires_at"] <= now() or not row["path"]
                or not (config.data / row["path"]).is_file()):
            raise AppError("draft_ineligible", "Выбранный образец недоступен или непригоден")
        rows.append(row)
    return rows


def close_clips(db, speaker_id):
    for clip in db.execute("SELECT path FROM review_clips WHERE speaker_id=?", (speaker_id,)):
        defer_unlink(db, clip["path"])
    db.execute(
        "UPDATE review_clips SET path=NULL,available=0,deleted_at=? WHERE speaker_id=? AND available=1",
        (now(), speaker_id),
    )


def apply_assignment(db, speaker, action, profile, name):
    state = {"reset": "needs_review", "skip": "skipped"}.get(action, "confirmed")
    reason = "Имя сброшено вручную" if action == "reset" else None
    source = "manual" if name else None
    db.execute(
        """UPDATE meeting_speakers SET status=?,reason=?,profile_id=?,name=?,source=?,version=version+1
           WHERE id=?""", (state, reason, profile, name, source, speaker["id"]),
    )
    if action != "reset":
        close_clips(db, speaker["id"])
    if (speaker["name"], speaker["profile_id"]) != (name, profile):
        db.execute(
            "UPDATE meetings SET transcript_revision=transcript_revision+1,updated_at=? WHERE id=?",
            (now(), speaker["meeting_id"]),
        )
        version = db.execute("SELECT transcript_revision FROM meetings WHERE id=?",
                             (speaker["meeting_id"],)).fetchone()[0]
        enqueue_summary(db, speaker["meeting_id"], version)
    return {"speaker_id": speaker["id"], "version": speaker["version"] + 1}


def resolve(config, db, meeting, speaker_id, payload):
    payload.check()
    speaker = speaker_row(db, meeting, speaker_id)
    verify_speaker(db, speaker, payload.expected_version)
    profile, name = assignment_target(db, speaker, payload)
    if not payload.save_voice:
        return 200, apply_assignment(db, speaker, payload.action, profile, name)
    chosen_clips(config, db, speaker, payload.clip_ids)
    job = add_job(db, "enroll_voice", meeting=meeting, speaker=speaker_id, profile=profile,
                  version=speaker["version"], params={"resolution": payload.model_dump()})
    return 202, {"job_id": job, "status": "queued"}


def queue_enrollment(config, db, profile, payload):
    payload.check_name()
    payload.check_consent()
    if profile:
        if payload.name is not None:
            raise AppError("unexpected_name", "Имя берётся из выбранного профиля")
        profile_row(db, profile)
    else:
        payload.confirmed_name()
    chosen_drafts(config, db, payload.draft_ids)
    job = add_job(db, "enroll_voice", profile=profile,
                  params={**payload.model_dump(), "profile_id": profile})
    return 202, {"job_id": job, "status": "queued"}


def expire_draft(db, draft):
    defer_unlink(db, draft["path"])
    db.execute("UPDATE voice_sample_drafts SET status='expired',path=NULL WHERE id=?", (draft["id"],))


def vector_blob(vector):
    if not isinstance(vector, list) or len(vector) != VOICE_DIM:
        raise AppError("invalid_embedding", "Модель вернула отпечаток неверной размерности")
    if any(not isinstance(value, (float, int)) or not math.isfinite(value) for value in vector):
        raise AppError("invalid_embedding", "Модель вернула непригодный голосовой отпечаток")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 0:
        raise AppError("invalid_embedding", "Модель вернула непригодный голосовой отпечаток")
    return struct.pack(f"<{VOICE_DIM}f", *(value / norm for value in vector))


def save_embeddings(db, profile, name, records, blobs, source, consent_version):
    if profile:
        profile_row(db, profile)
    else:
        profile = uid()
        db.execute("INSERT INTO speaker_profiles VALUES (?,?,?)", (profile, name, now()))
    consent_id, stamp = uid(), now()
    db.execute("INSERT INTO voice_consents VALUES (?,?,?,?,?,?,?)",
               (consent_id, profile, stamp, consent_version, "speaker_recognition", source,
                dump([row["id"] for row in records])))
    revision = model_lock()["wespeaker"]["revision"]
    db.executemany(
        "INSERT INTO speaker_embeddings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(uid(), profile, consent_id, blob, VOICE_DIM, VOICE_MODEL, revision, PREPROCESS, source,
          row["quality"], stamp) for row, blob in zip(records, blobs, strict=True)],
    )
    return profile
