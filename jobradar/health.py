"""Health control for sources.

A scraping service can report success and return zero records when the
target site changes its layout. A page fetcher can return a cookie banner
instead of the page. A message export can produce an empty file. All three
cases look like "nothing new today".
"""
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

DROP_RATIO = 0.30
ZERO_RUNS_TO_ALERT = 2


def record_run(conn: sqlite3.Connection, source: str, items: int, ok: bool) -> None:
    """Writes one source run to the database."""
    conn.execute(
        "INSERT INTO run_stats(source, items, ok, ts) VALUES(?,?,?,?)",
        (source, items, 1 if ok else 0,
         datetime.now(timezone.utc).isoformat(timespec="microseconds")),
    )
    conn.commit()


def median_items(conn: sqlite3.Connection, source: str, days: int = 7) -> float | None:
    """Returns the median number of records for the last N days, or None if there is no history."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")
    rows = conn.execute(
        "SELECT items FROM run_stats WHERE source=? AND ts>=? ORDER BY ts",
        (source, since),
    ).fetchall()
    values = [r[0] for r in rows]
    return statistics.median(values) if values else None


def check_health(conn: sqlite3.Connection, sources: list[str]) -> list[str]:
    """Checks the health of sources and returns a list of alerts."""
    alerts: list[str] = []
    for src in sources:
        # `rowid` in the sort is required: without it, when timestamps are
        # equal, the order of the "last runs" is not guaranteed, and the rule
        # could look at the wrong records.
        rows = conn.execute(
            "SELECT items FROM run_stats WHERE source=? "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (src, ZERO_RUNS_TO_ALERT),
        ).fetchall()
        recent = [r[0] for r in rows]
        if len(recent) >= ZERO_RUNS_TO_ALERT and all(v == 0 for v in recent):
            alerts.append(f"source {src} is dead: two runs in a row with zero items")
            continue
        med = median_items(conn, src)
        if med and recent and recent[0] < med * DROP_RATIO:
            alerts.append(
                f"source {src} dropped: {recent[0]} against a median of {med:.0f}")
    return alerts
