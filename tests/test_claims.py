"""Claims: parallel workers must never get the same event."""

from datetime import datetime, timedelta, timezone

from parkrun_monitoring import claims, db


def _seed(conn, n=3):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO countries (code, url, first_seen, last_seen) "
        "VALUES (97, 'www.parkrun.org.uk', ?, ?)",
        (now, now),
    )
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO events (id, eventname, country_code, series_id, "
            "is_active, first_seen, last_seen) VALUES (?, ?, 97, 1, 1, ?, ?)",
            (i, f"event{i}", now, now),
        )
    conn.commit()


def test_two_workers_get_different_events(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn)
    first = claims.claim_next_event(conn, "w1", ttl_minutes=60)
    second = claims.claim_next_event(conn, "w2", ttl_minutes=60)
    assert first and second and first != second


def test_release_frees_the_event(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, n=1)
    name = claims.claim_next_event(conn, "w1", ttl_minutes=60)
    assert claims.claim_next_event(conn, "w2", ttl_minutes=60) is None
    claims.release_event(conn, "w1", name)
    assert claims.claim_next_event(conn, "w2", ttl_minutes=60) == name


def test_foreign_release_is_ignored(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, n=1)
    name = claims.claim_next_event(conn, "w1", ttl_minutes=60)
    claims.release_event(conn, "w2", name)  # not the owner
    assert claims.claim_next_event(conn, "w2", ttl_minutes=60) is None


def test_expired_claim_is_reclaimed(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, n=1)
    name = claims.claim_next_event(conn, "w1", ttl_minutes=60)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=90)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn.execute("UPDATE events SET claimed_at=? WHERE eventname=?", (stale, name))
    conn.commit()
    assert claims.claim_next_event(conn, "w2", ttl_minutes=60) == name


def test_drained_queue_returns_none(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, n=2)
    assert claims.claim_next_event(conn, "w1", ttl_minutes=60)
    assert claims.claim_next_event(conn, "w1", ttl_minutes=60)
    assert claims.claim_next_event(conn, "w1", ttl_minutes=60) is None


def test_first_pass_only_stops_after_all_synced(tmp_path):
    """С first_pass_only воркер берёт только непройденные локации и
    останавливается (None), а не гоняет уже собранные по кругу."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.connect(tmp_path / "t.db")
    _seed(conn, n=2)
    # Пройти обе локации (проставить history_synced_at) и снять claim.
    for w in ("w1", "w2"):
        name = claims.claim_next_event(conn, w, ttl_minutes=60, first_pass_only=True)
        conn.execute(
            "UPDATE events SET history_synced_at=?, claimed_by=NULL, claimed_at=NULL "
            "WHERE eventname=?",
            (now, name),
        )
        conn.commit()
    # Непройденных не осталось → в first_pass_only режиме очередь пуста.
    assert claims.claim_next_event(conn, "w3", ttl_minutes=60, first_pass_only=True) is None
    # А в обычном режиме та же БД отдала бы уже пройденную (обновление истории).
    assert claims.claim_next_event(conn, "w3", ttl_minutes=60) is not None
