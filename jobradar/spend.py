"""Spend tracking for the paid steps of the contact search, and a budget brake.

Without tracking there is no way to see which paid step pays off and which
one burns money for nothing. Without a cap, one broken loop (a resolver stuck
on one company because of a pagination bug) eats the monthly budget in one
night. The planned spend is about $0.23 per day ($7 per month). The cap of
$10 per month is a safety margin for the unexpected, not a target.
"""
import sqlite3

DEFAULT_MONTHLY_CAP_USD = 10.0


def log_resolution(conn: sqlite3.Connection, company_id: int | None, rung: str,
                   found: int, cost_usd: float, ts: str) -> None:
    """Writes one attempt of one step into resolution_log.

    EVERY attempt is written, including free ones (cost_usd=0) and ones
    with no result (found=0). Without the free steps the report cannot tell
    "the cache did not run in this run" from "the cache ran and found
    nothing", and this difference is the reason the table exists.

    ts is an ISO string passed in by the caller. datetime.now() is not called
    here on purpose: without an external ts, the monthly sum test would
    depend on the calendar date of the test run.
    """
    conn.execute(
        "INSERT INTO resolution_log(company_id, rung, found, cost_usd, ts) "
        "VALUES (?,?,?,?,?)",
        (company_id, rung, found, cost_usd, ts),
    )
    conn.commit()


def month_spend(conn: sqlite3.Connection, month: str) -> float:
    """Sum of cost_usd for a month (the month is cut from ts by the YYYY-MM prefix).

    substr(ts,1,7) works both for a short ts like "2026-08-25" (10 chars) and
    for a full ISO "2026-08-25T10:00:00". The YYYY-MM prefix is the same in
    both cases, so the ts format does not matter to this module.

    SQL SUM over an empty set returns NULL, not 0. This is standard SQL
    behavior, not a bug. Without the explicit switch to 0.0 the callers
    (budget_left, paid_allowed) would get None and fail on arithmetic.
    """
    total = conn.execute(
        "SELECT SUM(cost_usd) FROM resolution_log WHERE substr(ts, 1, 7) = ?",
        (month,),
    ).fetchone()[0]
    return float(total) if total is not None else 0.0


def budget_left(conn: sqlite3.Connection, cfg: dict, month: str) -> float:
    """What is left of the monthly cap, never below zero.

    The config may not have a "spend" section yet. The module must not fail
    in that case. The default cap is 10.0 (the plan spends about $7 per
    month, $10 is the margin).
    """
    cap = cfg.get("spend", {}).get("monthly_cap_usd", DEFAULT_MONTHLY_CAP_USD)
    return max(0.0, cap - month_spend(conn, month))


def paid_allowed(conn: sqlite3.Connection, cfg: dict, month: str, cost: float) -> bool:
    """Whether another `cost` can be spent without going over the cap.

    The limit is inclusive: "spent 9.99, cost 0.01" gives exactly 10.0, still
    ALLOWED. "Spent 10.0, cost 0.01" gives 10.01, over the cap, NOT allowed.
    Checked in python3: 9.99 + 0.01 gives exactly 10.0 (IEEE-754 double
    rounds it exactly), so the comparison here is direct, without an
    epsilon. Rounding to cents would add a bigger error than the one it
    fixes: some paid APIs price calls in thousandths of a dollar ($0.003),
    and rounding would turn those prices into zero.
    """
    cap = cfg.get("spend", {}).get("monthly_cap_usd", DEFAULT_MONTHLY_CAP_USD)
    return month_spend(conn, month) + cost <= cap
