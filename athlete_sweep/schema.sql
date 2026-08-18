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

-- ГЛАВНЫЙ индекс очереди (07.08.2026). Запрос claim в worker.py фильтрует по
-- status='pending', но сортирует по athlete_id — и планировщик из-за ORDER BY
-- шёл по crawl_queue_pkey с начала таблицы, отбрасывая уже обработанные строки
-- фильтром. Замер на 2.4 млн собранных: «Rows Removed by Filter: 2 436 359»,
-- 813 мс на КАЖДЫЙ claim, и время росло линейно вместе с прогрессом обхода —
-- за неделю темп упал с 14 400 до 7 200 профилей в час.
--
-- Частичный индекс держит только pending-строки в порядке athlete_id: это
-- одновременно и фильтр, и готовая сортировка, поэтому claim берёт первую
-- запись сразу. Побочный бонус — индекс тает по мере обхода, а не растёт.
CREATE INDEX IF NOT EXISTS ix_queue_pending ON crawl_queue (athlete_id)
    WHERE status = 'pending';

-- Атлеты. Домашний парк НЕ храним (на странице его нет; при нужде — вычисляемое).
CREATE TABLE IF NOT EXISTS athletes (
    athlete_id         BIGINT PRIMARY KEY,
    name               TEXT,
    barcode            TEXT,
    age_category       TEXT,
    total_runs         INTEGER,
    is_russian_runner  BOOLEAN,          -- ХОТЯ БЫ ОДИН забег в РФ (не доля!) — эти уедут в БД сайта run5k.run
    status             TEXT NOT NULL,    -- класс страницы (см. crawl_queue.status)
    parsed_at          TIMESTAMPTZ,      -- реальная дата чтения страницы (для миграции — из легаси last_updated)
    source             TEXT NOT NULL DEFAULT 'crawl',  -- 'crawl' | 'legacy_migration'
    raw_html           TEXT,             -- только для status='unclassified' — на ревью Дмитрия
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_athletes_russian ON athletes (is_russian_runner) WHERE is_russian_runner;
CREATE INDEX IF NOT EXISTS ix_athletes_status ON athletes (status);

-- Провенанс отдельно от свежести (17.08.2026). `source` в worker.store() жёстко
-- перезаписывается на 'crawl' при КАЖДОЙ записи — значит после перепрохождения
-- легаси-атлетов признак «пришёл из легаси-выгрузки» был бы потерян. Поэтому
-- провенанс вынесен в отдельный флаг, который ставится один раз и не меняется:
--   legacy_seed=true,  source='legacy_migration' — легаси, ещё не перепройден
--   legacy_seed=true,  source='crawl'            — легаси, актуализирован обходом
--   legacy_seed=false, source='crawl'            — найден обходом с нуля
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS legacy_seed BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_athletes_legacy_seed ON athletes (legacy_seed) WHERE legacy_seed;

-- Опорные счётчики ДО перепрохождения легаси (17.08.2026): по ним считается,
-- сколько забегов и волонтёрств добавилось относительно сбора ноября 2025.
-- Полный снимок runs сознательно НЕ делался (решение Дмитрия) — только тальник:
--   CREATE TABLE legacy_baseline_2025_11 AS SELECT athlete_id, count(runs),
--   min/max(run_date), volunteer_summary.total_credits, athletes.parsed_at ...
-- Базовые числа: 55 139 атлетов, 735 734 забега, 158 662 волонтёрства.

-- Забеги атлета (полная история; event_slug — как на /all-странице).
--
-- У id НЕТ первичного ключа сознательно (07.08.2026). Настоящий ключ строки —
-- UNIQUE (athlete_id, event_slug, run_date): по нему идут все вставки
-- (ON CONFLICT в worker.py) и все чтения. Индекс же PRIMARY KEY (id) при
-- 40 млн строк весил 867 МБ и не обслужил НИ ОДНОГО запроса (idx_scan = 0 за
-- всю историю), при этом обновлялся на каждой вставке — а вставка здесь
-- круглосуточная. Ссылок на runs.id нет ни в коде, ни через внешние ключи
-- (FK ведёт по athlete_id), поэтому колонка осталась только как технический
-- счётчик. Если пересоздаёшь базу с нуля — не возвращай сюда PRIMARY KEY.
CREATE TABLE IF NOT EXISTS runs (
    id               BIGSERIAL,
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

-- Реестр VPN-выходов: per-IP задержка (лесенка), эскалирующий cooldown,
-- память об уровне бана (delay_floor растёт до n+1). Менеджер держит поток на
-- каждый enabled-выход, чей cooldown истёк.
CREATE TABLE IF NOT EXISTS sweep_exits (
    name           TEXT PRIMARY KEY,
    proxy          TEXT NOT NULL,
    kind           TEXT NOT NULL,               -- vless | hysteria2
    delay_sec      REAL NOT NULL DEFAULT 13,     -- старт 13с
    delay_floor    REAL NOT NULL DEFAULT 8,      -- пол (не ниже 8; растёт до n+1 после банов)
    cooldown_until TIMESTAMPTZ,
    ban_level      INTEGER NOT NULL DEFAULT 0,   -- индекс в лестнице 1ч..14д
    last_ok_at     TIMESTAMPTZ,
    last_waf_at    TIMESTAMPTZ,
    last_tuned_at  TIMESTAMPTZ,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE
);

-- Статистика капч по воркеру (добавлено 28.07.2026, для табло /hq).
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS captcha_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS captcha_solved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS last_captcha_at TIMESTAMPTZ;

-- Незакрывшиеся процессы браузера (добавлено 30.07.2026, диагностика утечки
-- Chromium — вместо вычитывания консоли на тысячах атлетов в час).
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS browser_close_fail_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS last_close_fail_at TIMESTAMPTZ;
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS last_close_fail_reason TEXT;

-- Описания событий (добавлено 31.07.2026, athlete_sweep/event_pages.py):
-- главная страница + /course/ каждой локации, секции контентных колонок.
CREATE TABLE IF NOT EXISTS event_pages (
    slug         TEXT NOT NULL,
    page         TEXT NOT NULL,           -- 'home' | 'course'
    url          TEXT NOT NULL,
    http_status  INTEGER,
    lang         TEXT,                    -- из <html lang=...>
    og_image     TEXT,                    -- фото события (og:image)
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (slug, page)
);
CREATE TABLE IF NOT EXISTS event_sections (
    slug          TEXT NOT NULL,
    page          TEXT NOT NULL,
    area          TEXT,                   -- 'left' | 'right' (колонка на странице)
    position      INTEGER NOT NULL,       -- порядок на странице
    heading_level INTEGER,                -- 2|3|4
    heading       TEXT,                   -- заголовок на языке страны
    canonical_key TEXT,                   -- what_is/when/where/course_description/…
    content_text  TEXT,                   -- только текст: html для анализа не нужен
    PRIMARY KEY (slug, page, position)
);
CREATE INDEX IF NOT EXISTS ix_event_sections_key ON event_sections (canonical_key);

