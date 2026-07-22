-- Staging-схема мирового обхода атлетов parkrun (отдельный Postgres, БД parkrun_world).
-- Импорт в прод run5k.run — ТОЛЬКО после завершения парсинга и по флагу is_russian_runner.
-- Проектировалось в диалоге 22.07.2026.

-- Очередь обхода диапазона ID (751355..7500000). Claim через FOR UPDATE SKIP LOCKED.
CREATE TABLE IF NOT EXISTS crawl_queue (
    athlete_id  BIGINT PRIMARY KEY,               -- parkrun athlete id
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending|ok|registered_empty|not_found|protected|unclassified|error
    claimed_by  TEXT,
    claimed_at  TIMESTAMPTZ,
    attempts    INTEGER NOT NULL DEFAULT 0,
    fetched_at  TIMESTAMPTZ,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_claim ON crawl_queue (status, claimed_at);

-- Атлеты. Домашний парк НЕ храним (на странице его нет; при нужде — вычисляемое).
CREATE TABLE IF NOT EXISTS athletes (
    athlete_id         BIGINT PRIMARY KEY,
    name               TEXT,
    barcode            TEXT,
    age_category       TEXT,
    total_runs         INTEGER,
    is_russian_runner  BOOLEAN,          -- >=50% забегов в РФ; считаем ПОСЛЕ парсинга истории
    status             TEXT NOT NULL,    -- класс страницы (см. crawl_queue.status)
    parsed_at          TIMESTAMPTZ,      -- реальная дата чтения страницы (для миграции — из легаси last_updated)
    source             TEXT NOT NULL DEFAULT 'crawl',  -- 'crawl' | 'legacy_migration'
    raw_html           TEXT,             -- только для status='unclassified' — на ревью Дмитрия
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_athletes_russian ON athletes (is_russian_runner) WHERE is_russian_runner;
CREATE INDEX IF NOT EXISTS ix_athletes_status ON athletes (status);

-- Забеги атлета (полная история; event_slug — как на /all-странице).
CREATE TABLE IF NOT EXISTS runs (
    id               BIGSERIAL PRIMARY KEY,
    athlete_id       BIGINT NOT NULL REFERENCES athletes(athlete_id) ON DELETE CASCADE,
    event_slug       TEXT NOT NULL,
    event_name       TEXT,
    run_date         DATE,
    run_number       INTEGER,
    position         INTEGER,
    finish_time_sec  INTEGER,
    age_grade        TEXT,
    is_pb            BOOLEAN,
    UNIQUE (athlete_id, event_slug, run_date)
);
CREATE INDEX IF NOT EXISTS ix_runs_athlete ON runs (athlete_id);
CREATE INDEX IF NOT EXISTS ix_runs_event ON runs (event_slug);

-- Волонтёрство — СУММА (одна строка на атлета): строка «итого учтённых волонтёрств».
CREATE TABLE IF NOT EXISTS volunteer_summary (
    athlete_id     BIGINT PRIMARY KEY REFERENCES athletes(athlete_id) ON DELETE CASCADE,
    total_credits  INTEGER NOT NULL
);

-- Волонтёрство — ДЕТАЛИ (по ролям/позициям): сколько раз на каждой позиции.
CREATE TABLE IF NOT EXISTS volunteer_detail (
    id          BIGSERIAL PRIMARY KEY,
    athlete_id  BIGINT NOT NULL REFERENCES athletes(athlete_id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    occasions   INTEGER NOT NULL,
    UNIQUE (athlete_id, role)
);
CREATE INDEX IF NOT EXISTS ix_voldetail_athlete ON volunteer_detail (athlete_id);
