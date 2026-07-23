-- Липкий пул бесплатных прокси для async-сборщика (free_collector.py).
-- Хранит валидированные адреса, чтобы после рестарта стартовать не с нуля и
-- переваливать уже-рабочие в первую очередь. account='free' в sweep_exits
-- больше не используется — этим пулом рулит отдельный async-процесс.
--
-- Ротация: прокси, который БЫЛ живой (last_ok_at IS NOT NULL), при бане/ошибках
-- НЕ удаляем — уводим в ступенчатую отлёжку (ban_level ↑, cooldown по лестнице) и
-- возвращаем после неё. Удаляем/ретайрим только тех, кто ни разу не ожил или
-- перебрал верх лестницы (persistently dead).
CREATE TABLE IF NOT EXISTS free_proxies (
    proxy          text PRIMARY KEY,          -- ip:port
    last_ok_at     timestamptz,               -- последний успешный parkrun-запрос
    last_fail_at   timestamptz,
    fails          int NOT NULL DEFAULT 0,     -- подряд неудач (сброс на успехе)
    ban_level      int NOT NULL DEFAULT 0,     -- ступень отлёжки (растёт при бане)
    cooldown_until timestamptz,                -- до этого момента прокси не берём
    latency_ms     int,
    collected_total int NOT NULL DEFAULT 0,    -- сколько атлетов спарсил (для табло)
    added_at       timestamptz NOT NULL DEFAULT now()
);
-- на случай апгрейда существующей таблицы
ALTER TABLE free_proxies ADD COLUMN IF NOT EXISTS collected_total int NOT NULL DEFAULT 0;
ALTER TABLE free_proxies ADD COLUMN IF NOT EXISTS active_seconds bigint NOT NULL DEFAULT 0;
ALTER TABLE free_proxies ADD COLUMN IF NOT EXISTS delay_sec real NOT NULL DEFAULT 35;
-- пер-бот счётчики для VPN-выходов (табло геймификации)
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS collected_total int NOT NULL DEFAULT 0;
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS active_seconds bigint NOT NULL DEFAULT 0;
-- heartbeat: менеджер метит выходы, которым реально выделен воркер (остальные
-- готовые, но простаивающие из-за лимита потоков, — «в очереди»)
ALTER TABLE sweep_exits ADD COLUMN IF NOT EXISTS worker_heartbeat_at timestamptz;
-- Активные для сбора: уже валидные и не в отлёжке.
CREATE INDEX IF NOT EXISTS ix_free_proxies_active ON free_proxies (last_ok_at DESC NULLS LAST)
    WHERE last_ok_at IS NOT NULL;
