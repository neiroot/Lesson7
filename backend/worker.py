import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import contextmanager

from backend import media, voices, yandex_disk
from backend.config import Config, OFFLINE_ENV, PREPROCESS, model_lock
from backend.db import AppError, connect, defer_unlink, dump, enqueue_summary, migrate, now, require_row, uid, unpack
from backend.metrics import peak_rss, worker_media
from backend.presentation import meeting_row, transcript
from backend.speech import Speech, exclusive_runtime
from backend.summary import Ollama
from backend.timing import asr_windows, normalize_intervals, review_windows, timeline


def load_calibration(config, revision):
    try:
        settings = json.loads(config.calibration.read_text())
        if (settings["model_revision"] == revision
                and settings["preprocess"] == PREPROCESS
                and settings["quality"] == {"min_rms": config.min_rms, "max_clipping": config.max_clipping}
                and -1 <= settings["threshold"] <= 1 and 0 < settings["margin"] <= 2
                and 5000 <= settings["min_speech_ms"] <= 20000
                and settings["validation"]["false_accepts"] == 0
                and settings["validation"]["correct_accepts"] > 0
                and settings["validation"]["negatives"] > 0):
            return settings
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def matches(scores, calibration, candidates, available):
    matched = {}
    reasons = {}
    for label, windows in candidates.items():
        ranking = sorted(((score, profile) for profile, score in scores.get(label, {}).items() if profile in available),
                         reverse=True)
        if not available:
            reasons[label] = "В голосовой базе пока нет образцов"
        elif not windows:
            reasons[label] = "Мало чистой речи для распознавания"
        elif not calibration:
            reasons[label] = "Автоимена включаются после калибровки"
        elif not ranking or ranking[0][0] < calibration["threshold"]:
            reasons[label] = "Уверенное совпадение не найдено"
        elif len(ranking) > 1 and ranking[0][0] - ranking[1][0] < calibration["margin"]:
            reasons[label] = "Найдено несколько похожих голосов"
        else:
            matched[label] = ranking[0][1]
    counts = Counter(matched.values())
    for label, profile in list(matched.items()):
        if counts[profile] > 1:
            reasons[label] = "На один профиль претендуют несколько спикеров"
            del matched[label]
    return matched, reasons


def system_memory():
    if os.uname().sysname != "Darwin":
        return {}
    result = {}
    for key, args in (
        ("swap", ["/usr/sbin/sysctl", "-n", "vm.swapusage"]),
        ("pressure", ["/usr/bin/memory_pressure", "-Q"]),
    ):
        try:
            run = subprocess.run(args, capture_output=True, text=True, timeout=2)
            if run.returncode == 0:
                result[key] = run.stdout.strip()[:400]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


class Worker:
    def __init__(self, config, speech=None, ollama=None):
        self.config = config
        self.speech = speech or Speech(config)
        self.ollama = ollama or Ollama(config)
        self.metrics = {}

    @contextmanager
    def measure(self, stage):
        before = system_memory()
        start = time.monotonic()
        rss = {}
        try:
            if stage in {"validate", "extract_audio", "check_sample"}:
                with peak_rss(worker_media) as rss:
                    yield
            else:
                yield
        finally:
            self.metrics[stage] = {"seconds": round(time.monotonic() - start, 3), **rss,
                                   "memory_before": before, "memory_after": system_memory()}

    def set_stage(self, meeting, stage, **values):
        with connect(self.config, write=True) as db:
            db.execute("UPDATE meetings SET stage=?,updated_at=? WHERE id=?", (stage, now(), meeting))
            if "duration" in values:
                db.execute("UPDATE meetings SET duration_ms=? WHERE id=?", (values["duration"], meeting))

    def prepare_video(self, job):
        meeting_id = job["meeting_id"]
        final = self.config.data / f"incoming/{meeting_id}.bin"
        import_id = unpack(job["params"], {}).get("cloud_import_id")
        if not import_id:
            return final
        if final.is_file():
            with connect(self.config, write=True) as db:
                db.execute("UPDATE cloud_imports SET status='processing',updated_at=? WHERE id=?",
                           (now(), import_id))
            return final
        pending = self.config.data / f"incoming/{meeting_id}.download"
        pending.unlink(missing_ok=True)
        self.set_stage(meeting_id, "downloading")
        with connect(self.config, write=True) as db:
            db.execute("UPDATE cloud_imports SET status='downloading',error=NULL,updated_at=? WHERE id=?",
                       (now(), import_id))
        try:
            with self.measure("download"):
                yandex_disk.download_import(self.config, import_id, pending)
            pending.replace(final)
        finally:
            pending.unlink(missing_ok=True)
        with connect(self.config, write=True) as db:
            db.execute("UPDATE cloud_imports SET status='processing',updated_at=? WHERE id=?",
                       (now(), import_id))
        return final

    def process_video(self, video_path, meeting_id):
        config = self.config
        self.ollama.ensure_released()
        work = config.data / "work" / meeting_id
        work.mkdir(mode=0o700, exist_ok=True)
        audio = work / "audio.pcm"
        created_clips = []
        published = False
        try:
            self.set_stage(meeting_id, "validate")
            with self.measure("validate"):
                duration, stream = media.validate_video(video_path)
            self.set_stage(meeting_id, "extract_audio", duration=duration)
            with self.measure("extract_audio"):
                media.extract(video_path, audio, stream)
                length = media.audio_length(audio)
            self.set_stage(meeting_id, "diarize")
            with self.measure("diarize"):
                output = self.speech.run("diarize", path=str(audio), timeout=None)
                regular = normalize_intervals(output["regular"], length)
                exclusive = normalize_intervals(output["exclusive"], length)
                spans = list(timeline(regular, exclusive))
            if not regular or not exclusive or not spans:
                raise AppError("no_speech", "В записи не найдено речи")
            labels = list(dict.fromkeys(label for label, _, _ in
                                        sorted(regular + exclusive, key=lambda value: (value[1], value[0]))))
            ids = {label: uid() for label in labels}
            self.set_stage(meeting_id, "identify")
            with self.measure("identify"):
                lock = model_lock()
                revision = lock["wespeaker"]["revision"]
                calibration = load_calibration(config, revision)
                clips = {}
                candidates = {}
                for label in labels:
                    clips[label] = []
                    candidates[label] = []
                    for start, end, clean in review_windows(spans, regular, label):
                        speech_ms, reasons = media.quality(audio, [(start, end)], config)
                        if not clean:
                            reasons.append("Есть наложение голосов или принадлежность речи не подтверждена")
                        clips[label].append((start, end, reasons, speech_ms))
                        if not reasons and speech_ms >= (calibration or {}).get("min_speech_ms", 5000):
                            candidates[label].append((start, end))
                with connect(config) as db:
                    has_profiles = db.execute("SELECT 1 FROM speaker_embeddings LIMIT 1").fetchone()
                scores = self.speech.run("identify", path=str(audio), db=str(config.db), revision=revision,
                                         candidates=candidates, timeout=None) if has_profiles and any(candidates.values()) else {}
            self.set_stage(meeting_id, "transcribe")
            with self.measure("transcribe"):
                windows = list(asr_windows(spans))
                texts = self.speech.run("asr", path=str(audio), windows=[(start, end) for start, end, _, _ in windows],
                                        timeout=None)
                segments = [(uid(), ids.get(label), start, end, int(overlap), text.strip())
                            for (start, end, label, overlap), text in zip(windows, texts, strict=True) if text.strip()]
            if not segments:
                raise AppError("no_transcript", "Модель не распознала речь в записи")
            self.set_stage(meeting_id, "finalize")
            with self.measure("finalize"):
                clip_records = []
                for label, records in clips.items():
                    for start, end, reasons, speech_ms in records:
                        clip_id = uid()
                        relative = f"clips/{clip_id}.wav"
                        target = config.data / relative
                        media.cut(audio, target, [(start, end)])
                        created_clips.append(target)
                        related = [{"id": segment, "partial": not (start <= begin and finish <= end)}
                                   for segment, speaker, begin, finish, _, _ in segments
                                   if speaker in {ids[label], None} and start < finish and end > begin]
                        clip_records.append((clip_id, meeting_id, ids[label], start, end, relative, 1,
                                             int(not reasons), dump({"reasons": reasons, "speech_ms": speech_ms}),
                                             dump(related), now()))
                with connect(config, write=True) as db:
                    profiles = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM speaker_profiles")}
                    matched, reasons = matches(scores, calibration, candidates, profiles)
                    for number, label in enumerate(labels, 1):
                        profile = matched.get(label)
                        db.execute(
                            """INSERT INTO meeting_speakers(id,meeting_id,label,number,status,reason,profile_id,
                               name,source,scores) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (ids[label], meeting_id, label, number, "auto_matched" if profile else "needs_review",
                             reasons.get(label), profile, profiles.get(profile), "auto" if profile else None,
                             dump({key: score for key, score in scores.get(label, {}).items() if key in profiles})),
                        )
                    for kind, intervals in (("regular", regular), ("exclusive", exclusive)):
                        db.executemany(
                            """INSERT INTO diarization_intervals(meeting_id,speaker_id,kind,start_ms,end_ms)
                               VALUES (?,?,?,?,?)""",
                            [(meeting_id, ids[label], kind, start, end) for label, start, end in intervals],
                        )
                    db.executemany(
                        """INSERT INTO transcript_segments(id,meeting_id,speaker_id,start_ms,end_ms,overlap,text)
                           VALUES (?,?,?,?,?,?,?)""",
                        [(sid, meeting_id, speaker, start, end, overlap, text)
                         for sid, speaker, start, end, overlap, text in segments],
                    )
                    db.executemany(
                        """INSERT INTO review_clips(id,meeting_id,speaker_id,start_ms,end_ms,path,available,
                           eligible,quality,segments,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", clip_records,
                    )
                    db.execute(
                        """UPDATE meetings SET transcript_revision=1,stage='summarize',model_versions=?,updated_at=?
                           WHERE id=?""",
                        (dump({key: item.get("revision", item.get("digest")) for key, item in lock.items()}),
                         now(), meeting_id),
                    )
                    enqueue_summary(db, meeting_id, 1)
                    db.execute(
                        """UPDATE jobs SET state='succeeded',result=?,updated_at=?
                           WHERE kind='process_video' AND meeting_id=? AND state='running'""",
                        (dump({"meeting_id": meeting_id}), now(), meeting_id),
                    )
                published = True
            return {"meeting_id": meeting_id}
        finally:
            if not published:
                for clip in created_clips:
                    clip.unlink(missing_ok=True)
            video_path.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)

    def check_sample(self, job):
        draft_id = job["draft_id"]
        config = self.config
        self.ollama.ensure_released()
        original = config.data / f"incoming/{draft_id}.bin"
        work = config.data / "work" / draft_id
        work.mkdir(mode=0o700, exist_ok=True)
        wav = work / "audio.wav"
        published = False
        target = config.data / f"drafts/{draft_id}.wav"
        try:
            with connect(config) as db:
                draft = require_row(db, "SELECT * FROM voice_sample_drafts WHERE id=?", (draft_id,))
                if draft["status"] != "checking" or draft["expires_at"] <= now():
                    raise AppError("draft_expired", "Черновик образца отменён или устарел", 409)
            stream = media.validate_sample(original, config.max_sample_seconds)
            media.extract(original, wav, stream, config.max_sample_seconds + 1)
            output = self.speech.run("diarize", path=str(wav))
            length = media.audio_length(wav)
            intervals = normalize_intervals(output["regular"], length)
            speakers = {label for label, _, _ in intervals}
            windows = []
            for start, end, _, _, normal in timeline(intervals, []):
                if normal:
                    windows.append((start, end))
            speech_ms, reasons = media.quality(wav, windows, config)
            if len(speakers) != 1:
                reasons.append("Нужна запись с одним говорящим" if speakers else "Речь не найдена")
            media.cut(wav, target, [(0, length)])
            with connect(config, write=True) as db:
                changed = db.execute(
                    """UPDATE voice_sample_drafts SET path=?,status=?,quality=?,speech_ms=?
                       WHERE id=? AND status='checking' AND expires_at>?""",
                    (f"drafts/{draft_id}.wav", "rejected" if reasons else "eligible",
                     dump({"reasons": reasons, "speech_ms": speech_ms, "windows": windows}),
                     speech_ms, draft_id, now()),
                ).rowcount
                if not changed:
                    raise AppError("draft_expired", "Черновик образца отменён или устарел", 409)
                result = {"draft_id": draft_id}
                self.finish(db, job, result)
            published = True
            return result
        finally:
            if not published:
                target.unlink(missing_ok=True)
            original.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)

    def enrollment_input(self, db, job):
        config = self.config
        params = unpack(job["params"])
        if job["speaker_id"]:
            resolution = voices.Resolution.model_validate(params["resolution"])
            resolution.check()
            speaker = voices.speaker_row(db, job["meeting_id"], job["speaker_id"])
            voices.verify_speaker(db, speaker, job["expected_version"], active_job=job["id"])
            profile, name = voices.assignment_target(db, speaker, resolution)
            records = voices.chosen_clips(config, db, speaker, resolution.clip_ids)
            samples = [{"path": str(config.data / row["path"]), "windows": [(0, row["end_ms"] - row["start_ms"])]}
                       for row in records]
            return profile, name, records, samples, speaker, resolution.action, resolution.consent_text_version
        enrollment = voices.Enrollment.model_validate({key: value for key, value in params.items()
                                                       if key != "profile_id"})
        enrollment.check_name()
        enrollment.check_consent()
        profile = params["profile_id"]
        name = voices.profile_row(db, profile)["name"] if profile else enrollment.confirmed_name()
        records = voices.chosen_drafts(config, db, enrollment.draft_ids, active_job=job["id"])
        samples = [{"path": str(config.data / row["path"]), "windows": unpack(row["quality"])["windows"]}
                   for row in records]
        return profile, name, records, samples, None, None, enrollment.consent_text_version

    def enroll(self, job):
        self.ollama.ensure_released()
        with connect(self.config) as db:
            _, _, _, samples, _, _, _ = self.enrollment_input(db, job)
        vectors = self.speech.run("embed", samples=samples)
        if not isinstance(vectors, list) or len(vectors) != len(samples):
            raise AppError("invalid_embedding", "Не удалось получить отпечатки для всех образцов")
        blobs = [voices.vector_blob(vector) for vector in vectors]
        with connect(self.config, write=True) as db:
            profile, name, records, current_samples, speaker, action, version = self.enrollment_input(db, job)
            if samples != current_samples:
                raise AppError("sample_changed", "Образцы изменились во время обработки", 409)
            profile = voices.save_embeddings(db, profile, name, records, blobs,
                                             "meeting_clip" if speaker else "upload", version)
            if speaker:
                voices.apply_assignment(db, speaker, action, profile, name)
            else:
                for record in records:
                    voices.expire_draft(db, record)
            result = {"profile_id": profile, "speaker_id": job["speaker_id"]}
            self.finish(db, job, result)
        return result

    def summarize(self, job):
        meeting, version = job["meeting_id"], job["expected_version"]
        with connect(self.config, write=True) as db:
            row = meeting_row(db, meeting)
            if row["transcript_revision"] != version:
                return {"superseded": True}
            segments = transcript(db, meeting)
            db.execute("UPDATE meetings SET summary_status='running',updated_at=? WHERE id=?", (now(), meeting))
        with self.measure("summarize"):
            notes = self.ollama.summarize(segments)
        with connect(self.config, write=True) as db:
            published = self.set_current_summary(db, meeting, version, notes)
            result = {"meeting_id": meeting, **({} if published else {"superseded": True})}
            self.finish(db, job, result)
        return result

    @staticmethod
    def set_current_summary(db, meeting, version, notes):
        published = db.execute(
            """UPDATE meetings SET summary=?,summary_revision=?,summary_status='current',summary_error=NULL,
               status='ready',stage='finalize',error=NULL,updated_at=? WHERE id=? AND transcript_revision=?""",
            (dump(notes), version, now(), meeting, version),
        ).rowcount > 0
        if published:
            db.execute("UPDATE cloud_imports SET status='ready',error=NULL,updated_at=? WHERE meeting_id=?",
                       (now(), meeting))
        return published

    def claim(self):
        with connect(self.config, write=True) as db:
            row = db.execute("""SELECT * FROM jobs WHERE state='queued'
                ORDER BY CASE WHEN kind='summarize' THEN 0 ELSE 1 END,created_at,id LIMIT 1""").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (now(), row["id"]))
                if row["kind"] == "process_video":
                    cloud = unpack(row["params"], {}).get("cloud_import_id")
                    db.execute("UPDATE meetings SET status='processing',stage=?,updated_at=? WHERE id=?",
                               ("downloading" if cloud else "validate", now(), row["meeting_id"]))
            return row

    @staticmethod
    def finish(db, job, result):
        db.execute("UPDATE jobs SET state='succeeded',result=?,updated_at=? WHERE id=? AND state='running'",
                   (dump(result), now(), job["id"]))

    def fail(self, db, job, error):
        kind, meeting = job["kind"], job["meeting_id"]
        payload = dict(error.payload)
        if kind == "process_video":
            payload.setdefault("stage", meeting_row(db, meeting)["stage"])
            db.execute("UPDATE meetings SET status='error',error=?,updated_at=? WHERE id=?",
                       (dump(payload), now(), meeting))
            defer_unlink(db, f"incoming/{meeting}.bin")
            db.execute("UPDATE cloud_imports SET status='error',error=?,updated_at=? WHERE meeting_id=?",
                       (dump(payload), now(), meeting))
        elif kind == "summarize":
            payload.setdefault("stage", "summarize")
            db.execute(
                """UPDATE meetings SET summary_status='error',summary_error=?,updated_at=?,
                   status=CASE WHEN summary_revision IS NULL THEN 'error' ELSE status END,
                   error=CASE WHEN summary_revision IS NULL THEN ? ELSE error END
                   WHERE id=? AND transcript_revision=?""",
                (dump(payload), now(), dump(payload), meeting, job["expected_version"]),
            )
            db.execute("UPDATE cloud_imports SET status='error',error=?,updated_at=? WHERE meeting_id=?",
                       (dump(payload), now(), meeting))
        elif kind == "check_sample":
            db.execute("""UPDATE voice_sample_drafts SET status='rejected',quality=?
                          WHERE id=? AND status='checking'""",
                       (dump({"reasons": [payload["message"]]}), job["draft_id"]))
            defer_unlink(db, f"incoming/{job['draft_id']}.bin")
        db.execute("UPDATE jobs SET state='failed',error=?,updated_at=? WHERE id=? AND state='running'",
                   (dump(payload), now(), job["id"]))

    def cleanup(self, *, startup=False):
        config = self.config
        with connect(config, write=True) as db:
            for draft in db.execute("SELECT * FROM voice_sample_drafts WHERE expires_at<=? AND status<>'expired'",
                                    (now(),)).fetchall():
                voices.expire_draft(db, draft)
            for deletion in db.execute("SELECT path FROM pending_deletions").fetchall():
                try:
                    (config.data / deletion["path"]).unlink(missing_ok=True)
                except OSError:
                    continue
                db.execute("DELETE FROM pending_deletions WHERE path=?", (deletion["path"],))
            if startup:
                keep = {row[0] for row in db.execute(
                    """SELECT path FROM review_clips WHERE available=1 UNION ALL
                       SELECT path FROM voice_sample_drafts WHERE path IS NOT NULL"""
                )}
                for row in db.execute("SELECT kind,meeting_id,draft_id FROM jobs WHERE state='queued'"):
                    if row["kind"] == "process_video":
                        keep.add(f"incoming/{row['meeting_id']}.bin")
                    if row["kind"] == "check_sample":
                        keep.add(f"incoming/{row['draft_id']}.bin")
                for folder in ("incoming", "clips", "drafts"):
                    for path in (config.data / folder).iterdir():
                        if f"{folder}/{path.name}" not in keep:
                            path.unlink(missing_ok=True)
                for directory in (config.data / "work").iterdir():
                    if directory.is_dir():
                        shutil.rmtree(directory)

    def recover(self):
        with connect(self.config, write=True) as db:
            jobs = db.execute("SELECT * FROM jobs WHERE state='running'").fetchall()
            for job in jobs:
                if job["kind"] == "summarize":
                    current = meeting_row(db, job["meeting_id"])["transcript_revision"]
                    if current == job["expected_version"]:
                        db.execute("UPDATE jobs SET state='queued',updated_at=? WHERE id=?", (now(), job["id"]))
                        db.execute("UPDATE meetings SET summary_status='queued' WHERE id=?", (job["meeting_id"],))
                    else:
                        self.finish(db, job, {"superseded": True})
                elif job["kind"] == "process_video" and unpack(job["params"], {}).get("cloud_import_id"):
                    db.execute("UPDATE jobs SET state='queued',updated_at=? WHERE id=?", (now(), job["id"]))
                    db.execute("UPDATE meetings SET status='queued',stage='queued',error=NULL,updated_at=? WHERE id=?",
                               (now(), job["meeting_id"]))
                    db.execute("UPDATE cloud_imports SET status='queued',error=NULL,updated_at=? WHERE meeting_id=?",
                               (now(), job["meeting_id"]))
                else:
                    self.fail(db, job, AppError("interrupted", "Обработка прервалась при перезапуске"))
        self.cleanup(startup=True)

    def run_one(self):
        job = self.claim()
        if not job:
            return False
        self.metrics = {}
        self.speech.metrics.clear()
        self.ollama.metrics.clear()
        kind = job["kind"]
        try:
            if kind == "process_video":
                result = self.process_video(self.prepare_video(job), job["meeting_id"])
            elif kind == "check_sample":
                with self.measure("check_sample"):
                    result = self.check_sample(job)
            elif kind == "enroll_voice":
                with self.measure("enroll_voice"):
                    result = self.enroll(job)
            else:
                result = self.summarize(job)
            with connect(self.config, write=True) as db:
                self.finish(db, job, result)
        except Exception as error:
            if not isinstance(error, AppError):
                if isinstance(error, OSError) and error.errno == 28:
                    error = AppError("disk_full", "На диске закончилось свободное место")
                elif isinstance(error, sqlite3.Error):
                    error = AppError("database_failed", "Не удалось сохранить данные в SQLite")
                else:
                    error = AppError("processing_failed", "Не удалось завершить этап обработки")
            with connect(self.config, write=True) as db:
                self.fail(db, job, error)
        finally:
            self.cleanup()
            metrics = {"job_id": job["id"], "kind": kind, "stages": self.metrics,
                       "speech": self.speech.metrics, "ollama": self.ollama.metrics}
            print(dump(metrics), flush=True)
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    os.umask(0o077)
    for key, value in OFFLINE_ENV.items():
        os.environ[key] = value
    config = Config()
    migrate(config)
    with exclusive_runtime(config):
        worker = Worker(config)
        worker.recover()
        if args.ready_file:
            from pathlib import Path
            Path(args.ready_file).touch()
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        last_clean = time.monotonic()
        while not stopping:
            if not worker.run_one():
                if time.monotonic() - last_clean >= 30:
                    worker.cleanup()
                    last_clean = time.monotonic()
                time.sleep(0.4)


if __name__ == "__main__":
    try:
        main()
    except AppError as error:
        raise SystemExit(error.payload["message"]) from None
