from pathlib import Path

import pytest

from parkrun_monitoring import db
from parkrun_monitoring.fetch import _digits_to_seconds, parse_event_history
from parkrun_monitoring.history import pick_events_for_history

HISTORY_HTML = """
<tr class="Results-table-row" data-parkrun="1098" data-date="2026-07-11"
    data-finishers="1482" data-volunteers="74" data-male="Max GREEN"
    data-female="Lola GALBRAITH" data-maletime="1630" data-femaletime="1929">
  <td><a href="../1098">1098</a></td>
  <td><a href="/bushy/parkrunner/5016184/">Max GREEN</a></td>
  <td><a href="/bushy/parkrunner/1366252/">Lola GALBRAITH</a></td>
</tr>
<tr class="Results-table-row" data-parkrun="1" data-date="2004-10-02"
    data-finishers="13" data-volunteers="" data-male="Chris OWENS"
    data-female="" data-maletime="10203" data-femaletime="">
  <td><a href="/bushy/parkrunner/2/">Chris OWENS</a></td>
</tr>
"""


def test_parse_event_history():
    rows = parse_event_history(HISTORY_HTML)
    assert len(rows) == 2
    first = rows[0]
    assert first.run_number == 1098
    assert first.run_date == "2026-07-11"
    assert first.finishers == 1482
    assert first.volunteers == 74
    assert first.male_name == "Max GREEN"
    assert first.male_athlete_id == 5016184
    assert first.male_time_sec == 16 * 60 + 30
    assert first.female_athlete_id == 1366252
    old = rows[1]
    assert old.volunteers is None
    assert old.female_name is None
    assert old.female_time_sec is None
    assert old.male_time_sec == 1 * 3600 + 2 * 60 + 3


def test_digits_to_seconds():
    assert _digits_to_seconds("1630") == 990
    assert _digits_to_seconds("") is None
    assert _digits_to_seconds("59") == 59


@pytest.fixture
def conn(tmp_path: Path):
    return db.connect(tmp_path / "t.db")


def test_pick_events_prefers_never_synced_then_stalest(conn):
    now = "2026-07-18T00:00:00Z"
    conn.execute(
        "INSERT INTO countries (code, url, first_seen, last_seen) "
        "VALUES (97, 'www.parkrun.org.uk', ?, ?)", (now, now))
    for event_id, name, synced in [
        (1, "fresh", "2026-07-17T00:00:00Z"),
        (2, "stale", "2026-07-01T00:00:00Z"),
        (3, "never", None),
    ]:
        conn.execute(
            "INSERT INTO events (id, eventname, country_code, series_id, "
            "is_active, first_seen, last_seen, history_synced_at) "
            "VALUES (?, ?, 97, 1, 1, ?, ?, ?)",
            (event_id, name, now, now, synced))
    picked = [r["eventname"] for r in pick_events_for_history(conn, 3)]
    assert picked == ["never", "stale", "fresh"]
