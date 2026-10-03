import pytest

from jobradar.db import connect, init_schema
from jobradar.health import check_health, median_items, record_run


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_schema(c)
    return c


def test_median_of_recorded_runs(conn):
    for n in (10, 20, 30):
        record_run(conn, "job_board", n, True)
    assert median_items(conn, "job_board") == 20


def test_no_history_returns_none(conn):
    assert median_items(conn, "unknown") is None


def test_two_zero_runs_raise_alert(conn):
    for n in (10, 10, 10):
        record_run(conn, "job_board", n, True)
    record_run(conn, "job_board", 0, True)
    record_run(conn, "job_board", 0, True)
    alerts = check_health(conn, ["job_board"])
    assert any("job_board" in a for a in alerts)


def test_dead_rule_fires_without_any_history(conn):
    """Isolates the "two zeros in a row" rule.

    There is no history, so the median is zero and the drop rule cannot fire
    at all. If an alert comes, it comes from exactly the rule this task was
    written for. The old test got its alert from the drop rule and passed
    even when the "two zeros" rule was deleted completely.
    """
    record_run(conn, "job_board", 0, True)
    record_run(conn, "job_board", 0, True)
    assert median_items(conn, "job_board") == 0
    alerts = check_health(conn, ["job_board"])
    assert len(alerts) == 1
    assert "dead" in alerts[0]


def test_healthy_source_has_no_alert(conn):
    for n in (10, 12, 11, 13):
        record_run(conn, "job_board", n, True)
    assert check_health(conn, ["job_board"]) == []


def test_sharp_drop_raises_alert(conn):
    for _ in range(5):
        record_run(conn, "telegram", 100, True)
    record_run(conn, "telegram", 5, True)
    alerts = check_health(conn, ["telegram"])
    assert any("telegram" in a for a in alerts)
