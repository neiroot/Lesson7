CREATE TABLE cloud_integrations (
 provider TEXT PRIMARY KEY, status TEXT NOT NULL, folder TEXT NOT NULL,
 connected_at TEXT, last_check_at TEXT, last_success_at TEXT, last_error TEXT
);
CREATE TABLE oauth_attempts (
 state TEXT PRIMARY KEY, provider TEXT NOT NULL, verifier TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE cloud_imports (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, source_kind TEXT NOT NULL,
 source_key TEXT NOT NULL, remote_path TEXT, filename TEXT NOT NULL,
 mime_type TEXT, size INTEGER NOT NULL, revision TEXT NOT NULL,
 remote_sha256 TEXT, remote_md5 TEXT, modified TEXT,
 status TEXT NOT NULL, error TEXT, meeting_id TEXT REFERENCES meetings ON DELETE SET NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(provider, source_key, revision)
);
ALTER TABLE meetings ADD COLUMN source TEXT NOT NULL DEFAULT 'upload';
ALTER TABLE meetings ADD COLUMN cloud_import_id TEXT REFERENCES cloud_imports ON DELETE SET NULL;
CREATE INDEX cloud_imports_status ON cloud_imports(status, created_at);
CREATE INDEX cloud_imports_meeting ON cloud_imports(meeting_id);
DROP INDEX one_video;
CREATE UNIQUE INDEX one_video_running ON jobs(kind) WHERE kind='process_video' AND state='running';
