CREATE TABLE meetings (
 id TEXT PRIMARY KEY, filename TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 duration_ms INTEGER, status TEXT NOT NULL, stage TEXT, error TEXT,
 transcript_revision INTEGER NOT NULL DEFAULT 0, summary_revision INTEGER,
 summary_status TEXT NOT NULL DEFAULT 'not_started', summary_error TEXT, summary TEXT, model_versions TEXT
);
CREATE TABLE speaker_profiles (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE meeting_speakers (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings ON DELETE CASCADE,
 label TEXT NOT NULL, number INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT,
 profile_id TEXT REFERENCES speaker_profiles ON DELETE SET NULL, name TEXT, source TEXT,
 scores TEXT, version INTEGER NOT NULL DEFAULT 0,
 UNIQUE(meeting_id, label), UNIQUE(meeting_id, id)
);
CREATE TABLE diarization_intervals (
 id INTEGER PRIMARY KEY, meeting_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('regular','exclusive')), start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
 FOREIGN KEY(meeting_id, speaker_id) REFERENCES meeting_speakers(meeting_id, id) ON DELETE CASCADE
);
CREATE TABLE transcript_segments (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings ON DELETE CASCADE,
 speaker_id TEXT, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, overlap INTEGER NOT NULL, text TEXT NOT NULL,
 FOREIGN KEY(meeting_id, speaker_id) REFERENCES meeting_speakers(meeting_id, id) ON DELETE CASCADE
);
CREATE TABLE review_clips (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
 start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, path TEXT, available INTEGER NOT NULL,
 eligible INTEGER NOT NULL, quality TEXT NOT NULL, segments TEXT NOT NULL,
 created_at TEXT NOT NULL, deleted_at TEXT,
 FOREIGN KEY(meeting_id, speaker_id) REFERENCES meeting_speakers(meeting_id, id) ON DELETE CASCADE
);
CREATE TABLE voice_sample_drafts (
 id TEXT PRIMARY KEY, path TEXT, status TEXT NOT NULL, quality TEXT, speech_ms INTEGER,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE voice_consents (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES speaker_profiles ON DELETE CASCADE,
 created_at TEXT NOT NULL, text_version TEXT NOT NULL, purpose TEXT NOT NULL,
 source TEXT NOT NULL, source_ids TEXT NOT NULL, UNIQUE(profile_id, id)
);
CREATE TABLE speaker_embeddings (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES speaker_profiles ON DELETE CASCADE,
 consent_id TEXT NOT NULL, vector BLOB NOT NULL, dimension INTEGER NOT NULL, model_id TEXT NOT NULL,
 model_revision TEXT NOT NULL, preprocess_version TEXT NOT NULL, source TEXT NOT NULL,
 quality TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(profile_id, consent_id) REFERENCES voice_consents(profile_id, id) ON DELETE CASCADE
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, meeting_id TEXT REFERENCES meetings ON DELETE CASCADE,
 speaker_id TEXT REFERENCES meeting_speakers ON DELETE CASCADE, draft_id TEXT REFERENCES voice_sample_drafts,
 target_profile_id TEXT REFERENCES speaker_profiles ON DELETE SET NULL,
 expected_version INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 params TEXT NOT NULL, result TEXT, error TEXT
);
CREATE TABLE idempotency (
 key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL
);
CREATE TABLE pending_deletions (path TEXT PRIMARY KEY);
CREATE INDEX speakers_meeting_status ON meeting_speakers(meeting_id, status);
CREATE INDEX intervals_meeting ON diarization_intervals(meeting_id, kind, start_ms);
CREATE INDEX segments_meeting ON transcript_segments(meeting_id, start_ms, id);
CREATE INDEX clips_speaker ON review_clips(meeting_id, speaker_id);
CREATE INDEX embedding_profiles ON speaker_embeddings(profile_id);
CREATE INDEX jobs_state ON jobs(state, created_at);
CREATE INDEX jobs_meeting ON jobs(meeting_id, state);
CREATE INDEX jobs_speaker ON jobs(speaker_id, state);
CREATE UNIQUE INDEX one_video ON jobs(kind) WHERE kind='process_video' AND state IN ('queued','running');
CREATE UNIQUE INDEX one_summary ON jobs(meeting_id, expected_version)
 WHERE kind='summarize' AND state IN ('queued','running');
