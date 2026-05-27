-- =====================================================================
-- FrameExtractor — frames_db schema
-- =====================================================================
-- Runs once on first container start via
--   /docker-entrypoint-initdb.d/01_init_db.sql
-- Tables: video, job, frame
-- Role:   extractor_role (FastAPI app + worker)
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()


-- =====================================================================
-- Role
-- Dev-only password. Replace with Docker secrets in prod.
-- =====================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'extractor_role') THEN
        CREATE ROLE extractor_role LOGIN PASSWORD 'dev_extractor_pw';
    END IF;
END $$;


-- =====================================================================
-- Tables
-- =====================================================================

CREATE TABLE IF NOT EXISTS video (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    filename       VARCHAR(255) NOT NULL,
    file_path      TEXT         NOT NULL,            -- /data/media/<id>.<ext>
    container_ext  VARCHAR(16)  NOT NULL,            -- 'mp4', 'mov', 'avi', ...
    duration_sec   DOUBLE PRECISION,
    src_fps        DOUBLE PRECISION,
    width          INTEGER,
    height         INTEGER,
    size_bytes     BIGINT,
    uploaded_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id        UUID         NOT NULL REFERENCES video(id) ON DELETE CASCADE,
    params          JSONB        NOT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'queued'
                                  CHECK (status IN ('queued','running','done','failed','cancelled')),
    progress_pct    SMALLINT     NOT NULL DEFAULT 0,
    frames_total    INTEGER,
    frames_done     INTEGER      NOT NULL DEFAULT 0,
    error_message   TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS frame (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id       UUID         NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    video_id     UUID         NOT NULL REFERENCES video(id) ON DELETE CASCADE,
    frame_index  INTEGER      NOT NULL,
    time_sec     DOUBLE PRECISION NOT NULL,
    file_path    TEXT         NOT NULL,              -- /data/frames/<video_stem>/<video_stem>_NNNNN.png
    width        INTEGER      NOT NULL,
    height       INTEGER      NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- =====================================================================
-- Indexes
-- =====================================================================
CREATE INDEX IF NOT EXISTS idx_video_uploaded_at  ON video(uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_video          ON job(video_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_status         ON job(status);
CREATE INDEX IF NOT EXISTS idx_frame_job          ON frame(job_id, frame_index);
CREATE INDEX IF NOT EXISTS idx_frame_video        ON frame(video_id);


-- =====================================================================
-- Grants
-- =====================================================================
GRANT CONNECT ON DATABASE frames_db TO extractor_role;
GRANT USAGE   ON SCHEMA public      TO extractor_role;

GRANT SELECT, INSERT, UPDATE, DELETE ON video, job, frame TO extractor_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO extractor_role;
