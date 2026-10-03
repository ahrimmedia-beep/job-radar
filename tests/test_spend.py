import pytest

from jobradar.db import connect, init_schema
from jobradar.spend import budget_left, log_resolution, month_spend, paid_allowed


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_schema(c)
    return c


CFG = {"spend": {"monthly_cap_usd": 10.0}}


def test_month_spend_on_empty_log_is_zero(conn):
    """An empty log gives 0.0, not None: callers compare the result with
    numbers (the cap), and None would break the comparison with an exception."""
    assert month_spend(conn, "2026-08") == 0.0


def test_three_entries_sum_and_other_month_excluded(conn):
    """Three entries of one month are summed. An entry of the next month
    (even with a bigger amount) does not get into the total."""
    log_resolution(conn, None, "cache", 0, 1.0, "2026-08-01T09:00:00")
    log_resolution(conn, None, "website", 1, 2.5, "2026-08-15T12:00:00")
    log_resolution(conn, None, "paid_api", 1, 3.5, "2026-08-30T23:59:00")
    log_resolution(conn, None, "paid_api", 1, 100.0, "2026-07-31T23:59:00")
    assert month_spend(conn, "2026-08") == 7.0


def test_free_rung_is_logged_but_does_not_move_the_sum(conn):
    """The cache (cost_usd=0) costs nothing, but the attempt must stay visible
    in resolution_log. Otherwise the log cannot tell "the step did not run"
    from "the step ran for free", and that is exactly why the log exists."""
    log_resolution(conn, None, "cache", 1, 0.0, "2026-08-05T10:00:00")
    rows = conn.execute("SELECT rung, found, cost_usd FROM resolution_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["rung"] == "cache" and rows[0]["found"] == 1
    assert month_spend(conn, "2026-08") == 0.0


def test_paid_allowed_boundary_is_inclusive(conn):
    """The cap boundary: spent 9.99, cost 0.01, total exactly 10.0, still
    ALLOWED. Spent 10.0, another 0.01, total 10.01, over the cap, NOT allowed.
    Checked in python3: 9.99 + 0.01 == 10.0 exactly (IEEE-754 double), so this
    boundary needs no epsilon."""
    log_resolution(conn, None, "paid_api", 0, 9.99, "2026-08-10T00:00:00")
    assert paid_allowed(conn, CFG, "2026-08", 0.01) is True
    log_resolution(conn, None, "paid_api", 0, 0.01, "2026-08-10T00:01:00")
    assert paid_allowed(conn, CFG, "2026-08", 0.01) is False


def test_budget_left_does_not_go_negative_on_overspend(conn):
    """Cap 10, spent 15 (an overspend, for example a price fixed by hand after
    the fact): what is left is 0.0, not a negative number."""
    log_resolution(conn, None, "paid_api", 1, 15.0, "2026-08-01T00:00:00")
    assert budget_left(conn, CFG, "2026-08") == 0.0


def test_company_id_none_is_allowed_and_counted(conn):
    """company_id=None is allowed: the company may not have been resolved,
    but the money for the attempt is already spent and must get into the
    monthly total. Otherwise the spend becomes invisible."""
    log_resolution(conn, None, "paid_api", 0, 0.012, "2026-08-12T00:00:00")
    assert month_spend(conn, "2026-08") == pytest.approx(0.012)


def test_missing_spend_section_defaults_to_ten_dollars(conn):
    """The config has no "spend" section yet. The module must not fail, and
    the default cap is 10.0."""
    assert budget_left(conn, {}, "2026-08") == 10.0


def test_paid_allowed_reads_cap_from_config_not_the_default(conn):
    """The cap is read FROM THE CONFIG, not from the module constant.

    All other tests here use a cap of 10.0, the same number as
    DEFAULT_MONTHLY_CAP_USD, so an implementation that never reads the config
    would pass them. The owner's scenario: money is tight this month, the
    config gets `"monthly_cap_usd": 3.0`, and the brake must stop at three
    dollars, not at ten. paid_allowed is the valve that stops spending, so it
    is the function under test.
    """
    tight = {"spend": {"monthly_cap_usd": 3.0}}
    log_resolution(conn, None, "paid_api", 1, 2.99, "2026-08-03T00:00:00")
    assert paid_allowed(conn, tight, "2026-08", 0.01) is True
    assert paid_allowed(conn, tight, "2026-08", 0.02) is False
    # With the default cap of 10.0 the same data would allow both spends,
    # so the test tells the config from the constant, not only the boundary.
    assert paid_allowed(conn, {}, "2026-08", 0.02) is True
    assert budget_left(conn, tight, "2026-08") == pytest.approx(0.01)
