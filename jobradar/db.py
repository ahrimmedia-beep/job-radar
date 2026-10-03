"""SQLite is the source of truth. One file, no ORM.

This is the part of the schema that the published modules use.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);

CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY, name TEXT, role TEXT,
    company_id INTEGER REFERENCES company(id));

CREATE TABLE IF NOT EXISTS identity (
    kind TEXT NOT NULL, value_norm TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES person(id),
    PRIMARY KEY (kind, value_norm));

CREATE TABLE IF NOT EXISTS run_stats (
    source TEXT NOT NULL, items INTEGER NOT NULL,
    ok INTEGER NOT NULL, ts TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, times INTEGER DEFAULT 1);

-- What was already delivered to the owner. Kept apart from seen: seen counts
-- appearances in a source (how often a vacancy is reposted), delivered counts
-- what went into the digest. Without this table the evening run sends the
-- morning cards again.
CREATE TABLE IF NOT EXISTS delivered (
    key TEXT PRIMARY KEY, delivered_at TEXT NOT NULL);

-- Which step of the contact search gave what, and what it cost. Without this
-- there is no way to see which steps pay off and which only burn money.
CREATE TABLE IF NOT EXISTS resolution_log (
    company_id INTEGER REFERENCES company(id), rung TEXT NOT NULL,
    found INTEGER NOT NULL, cost_usd REAL NOT NULL DEFAULT 0, ts TEXT NOT NULL);

-- A person can work for SEVERAL companies. `person.company_id` stays and keeps
-- the first link (existing code reads it), and the full picture lives here.
-- Without this table a recruiter seen at three companies is listed under one,
-- and the other two look resolved but have zero contacts.
--
-- What this table does NOT allow: merging two different people. The key of a
-- person is still only (kind, value_norm) in `identity`, and the rule "when in
-- doubt, do not merge" is not relaxed at all. Only one thing is relaxed:
-- "a person has exactly one company".
CREATE TABLE IF NOT EXISTS person_company (
    person_id INTEGER NOT NULL REFERENCES person(id),
    company_id INTEGER NOT NULL REFERENCES company(id),
    first_seen_at TEXT,
    PRIMARY KEY (person_id, company_id));

CREATE INDEX IF NOT EXISTS idx_resolution_company ON resolution_log(company_id);
CREATE INDEX IF NOT EXISTS idx_person_company_company ON person_company(company_id);
"""

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will not
# add them: the production database already exists and is full of data, and
# recreating it would lose delivery marks and repost counts.
ADDED_COLUMNS = (
    # The contact "as it appeared" (before lower()). value_norm is case
    # insensitive for telegram and email, but some LinkedIn profile IDs are
    # case sensitive, and without this column the link was lost for good:
    # the database kept the lower case form, which points to a page that
    # does not exist.
    ("identity", "value_raw", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # no such table at all; SCHEMA above will create it
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _backfill_person_company(conn: sqlite3.Connection) -> None:
    """Copies existing `person.company_id` links into `person_company`.

    Same case as ADDED_COLUMNS, but here data moves, not columns: the
    production database is full of people linked by a single column, while
    the cache step of the contact search starts reading the link table. If
    the links were not copied, the cache on the server would wake up empty
    and the search would pay again for people who are already in the database.

    `INSERT OR IGNORE` instead of "run once behind a flag": init_schema runs
    on every start (twice a day), and a repeat must be harmless. This also
    heals itself: if some code writes `person.company_id` without going
    through `dedup.upsert_person` (tests do this), the link is picked up on
    the next start instead of being lost. Links that exist ONLY in
    person_company (the second and third company of a person) are not
    touched. The backfill deletes nothing.

    first_seen_at of copied links stays NULL on purpose: nobody knows when
    the link really appeared, and the migration date would be a lie. NULL
    sorts first in ascending order, which is true for the oldest links.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(person)")}
    if "company_id" not in have:
        return  # no person table, or no such column: nothing to copy
    conn.execute(
        "INSERT OR IGNORE INTO person_company(person_id, company_id, first_seen_at) "
        "SELECT id, company_id, NULL FROM person WHERE company_id IS NOT NULL"
    )


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    _backfill_person_company(conn)
    conn.commit()
