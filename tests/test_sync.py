import sqlite3
from pathlib import Path

import pytest

from parkrun_monitoring import db
from parkrun_monitoring.fetch import CatalogueEvent
from parkrun_monitoring.sync import ChangeSet, _field_diff, sync_catalogue
from parkrun_monitoring.notify import format_message


def make_event(event_id=1, eventname="bushy", latitude=51.41, **overrides):
    values = dict(
        id=event_id,
        eventname=eventname,
        long_name="Bushy parkrun",
        short_name="Bushy Park",
        localised_long_name=None,
        country_code=97,
        series_id=1,
        location="Bushy Park, Teddington",
        latitude=latitude,
        longitude=-0.335791,
    )
    values.update(overrides)
    return CatalogueEvent(**values)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "test.db")


def catalogue_sync(conn, monkeypatch, events):
    """Run sync_catalogue against a stubbed events.json feed."""
    import parkrun_monitoring.sync as sync_module

    monkeypatch.setattr(
        sync_module, "fetch_catalogue", lambda client: ([], events)
    )
    changes = sync_catalogue(conn, client=None)
    conn.commit()
    return changes


def test_first_import_is_baseline_not_changes(conn, monkeypatch):
    changes = catalogue_sync(conn, monkeypatch, [make_event()])
    assert changes.initial_import
    assert not changes.has_catalogue_changes
    assert conn.execute("SELECT COUNT(*) FROM event_changes").fetchone()[0] == 0


def test_added_removed_reappeared(conn, monkeypatch):
    catalogue_sync(conn, monkeypatch, [make_event(1)])

    changes = catalogue_sync(conn, monkeypatch, [make_event(1), make_event(2, "newpark")])
    assert [e.eventname for e in changes.added] == ["newpark"]

    changes = catalogue_sync(conn, monkeypatch, [make_event(2, "newpark")])
    assert [r["eventname"] for r in changes.removed] == ["bushy"]
    row = conn.execute("SELECT is_active, disappeared_at FROM events WHERE id=1").fetchone()
    assert row["is_active"] == 0 and row["disappeared_at"]

    changes = catalogue_sync(conn, monkeypatch, [make_event(1), make_event(2, "newpark")])
    assert [e.eventname for e in changes.reappeared] == ["bushy"]
    assert conn.execute("SELECT is_active FROM events WHERE id=1").fetchone()[0] == 1


def test_modified_records_field_diff(conn, monkeypatch):
    catalogue_sync(conn, monkeypatch, [make_event()])
    changes = catalogue_sync(conn, monkeypatch, [make_event(latitude=51.5)])
    assert len(changes.modified) == 1
    _, diff = changes.modified[0]
    assert list(diff) == ["latitude"]


def test_coordinate_jitter_below_epsilon_is_ignored(conn, monkeypatch):
    catalogue_sync(conn, monkeypatch, [make_event(latitude=51.41)])
    changes = catalogue_sync(conn, monkeypatch, [make_event(latitude=51.4100000001)])
    assert not changes.modified


def test_format_message_none_when_no_changes():
    assert format_message(ChangeSet(events_total=10)) is None


def test_format_message_lists_changes():
    changes = ChangeSet(events_total=10, added=[make_event(2, "newpark")])
    text = format_message(changes)
    assert "newpark" in text and "Появились (1)" in text


def test_check_gate(monkeypatch):
    from parkrun_monitoring.config import Config
    from parkrun_monitoring.sync import check_gate
    from pathlib import Path

    def cfg(command):
        return Config(
            db_path=Path("/tmp/x.db"), user_agent="ua", request_delay=0,
            vk_token=None, vk_peer_id=None, gate_command=command,
            push_command=None, history_delay=0,
        )

    assert check_gate(cfg(None)) is None
    assert check_gate(cfg("true")) is None
    reason = check_gate(cfg("echo banned until 99; false"))
    assert reason and "banned until 99" in reason


def test_country_names_are_filled_and_new_ones_reported(conn, monkeypatch, capsys):
    import parkrun_monitoring.sync as sync_module
    from parkrun_monitoring.fetch import CatalogueCountry

    monkeypatch.setattr(
        sync_module,
        "fetch_catalogue",
        lambda client: ([CatalogueCountry(97, "www.parkrun.org.uk", None),
                         CatalogueCountry(999, "www.parkrun.example", None)], []),
    )
    sync_module.sync_catalogue(conn, client=None)
    names = dict(conn.execute("SELECT code, name FROM countries").fetchall())
    assert names[97] == "United Kingdom"
    assert names[999] is None
    assert "country 999" in capsys.readouterr().out
