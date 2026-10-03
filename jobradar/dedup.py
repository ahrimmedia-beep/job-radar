"""People are merged only by deterministic keys.

Order of strength: linkedin slug -> personal email -> telegram handle.
Full name and company name are not keys: twenty percent of duplicates cost
less than one false merge, after which the owner writes "I saw your post"
to the wrong person.
"""
import hashlib
import re
import sqlite3
from datetime import datetime, timezone

from .contacts import is_role_mailbox
from .models import Contact

_ORDER = {"linkedin": 0, "email": 1, "telegram": 2}


def identity_keys(contacts: list[Contact]) -> list[tuple[str, str]]:
    """Keys in order of decreasing reliability.

    The second element of the sort key is required: without it the order
    INSIDE one kind (two telegram handles in one text) would depend on
    PYTHONHASHSEED, and on the same data a record could be attached to
    different people in different runs. Checked: with seed 7 and seed 99 the
    order was different. For a module whose whole point is deterministic
    merging, this is not acceptable.
    """
    keys = [
        (c.kind, c.value_norm)
        for c in contacts
        if c.kind in _ORDER and not is_role_mailbox(c)
    ]
    return sorted(set(keys), key=lambda k: (_ORDER[k[0]], k[1]))


def posting_key(source: str, external_id: str) -> str:
    """Builds the key used to track how often a vacancy is shown."""
    return f"{source}:{external_id}"


def content_key(source: str, text: str) -> str:
    """Content fingerprint: recognizes the same vacancy under a new ID.

    Counting by message ID makes no sense: every Telegram repost has a new
    ID, and the counter is always one. Links and punctuation are removed,
    because they change most often in a repost. Latin and Cyrillic letters
    are kept, since posts come in both scripts.

    Limitation: a translation of the same post into another language gives
    another fingerprint. A company that posts one vacancy in two languages
    shows up as two vacancies.
    """
    t = re.sub(r"https?://\S+", "", text.lower())
    t = re.sub(r"[^a-z0-9 \u0430-\u044f\u0451]", " ", t)
    digest = hashlib.sha1(" ".join(t.split())[:400].encode()).hexdigest()[:16]
    return f"{source}:{digest}"


def mark_seen(conn: sqlite3.Connection, key: str, day: str | None = None) -> int:
    """Registers an appearance and returns the number of DIFFERENT DAYS it happened.

    Counting runs is wrong: the script runs twice a day, and more often while
    debugging, and then "reposted 5 times" only means five runs.
    """
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("SELECT first_seen_at, times FROM seen WHERE key=?", (key,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO seen(key, first_seen_at, times) VALUES(?,?,1)", (key, day))
        conn.commit()
        return 1
    if row[0] and row[0].startswith(day):
        return row[1]
    seen_days = conn.execute(
        "SELECT times FROM seen WHERE key=?", (key,)).fetchone()[0]
    conn.execute("UPDATE seen SET times=?, first_seen_at=? WHERE key=?",
                 (seen_days + 1, day, key))
    conn.commit()
    return seen_days + 1


def is_delivered(conn: sqlite3.Connection, key: str) -> bool:
    """Whether this record was ever delivered to the owner."""
    return conn.execute("SELECT 1 FROM delivered WHERE key=?", (key,)).fetchone() is not None


def mark_delivered(conn: sqlite3.Connection, keys: list[str], ts: str) -> None:
    """Marks records as delivered.

    Called ONLY after a confirmed send: a digest that was not delivered must
    not burn its cards. Otherwise a network failure silently eats the day's
    results and the owner never learns about them.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO delivered(key, delivered_at) VALUES(?,?)",
        [(k, ts) for k in keys],
    )
    conn.commit()


def find_person(conn: sqlite3.Connection, keys: list[tuple[str, str]]) -> int | None:
    """Looks up an existing person by their deterministic keys.

    Searches in priority order: if any key is found in the identity table,
    returns its person_id.
    """
    for kind, value in keys:
        row = conn.execute(
            "SELECT person_id FROM identity WHERE kind=? AND value_norm=?",
            (kind, value),
        ).fetchone()
        if row:
            return row[0]
    return None


def upsert_person(conn: sqlite3.Connection, name: str | None, role: str | None,
                  contacts: list[Contact], company_id: int | None) -> int:
    """Creates or updates a person record.

    If a record already exists for the contact keys, it is updated.
    If not, a new one is created. In both cases the contacts are registered
    in the identity table.

    Name and role: an empty value is filled in, a filled value is kept
    (COALESCE(?, ...): the argument wins). Company: the OPPOSITE,
    COALESCE(company_id, ?): the stored value wins over the argument. The
    argument order in the two COALESCE calls next to each other differs on
    purpose. It is not a typo.

    Why the company behaves differently. `identity` is unique globally
    ((kind, value_norm) is the primary key), so one contact is exactly one
    `person`. The old COALESCE(?, company_id) only replaced NULL, and with a
    non-empty argument it OVERWROTE the company that was already set. The
    contact search calls this function with company_id on EVERY find. This
    was reproduced on real data: a person started at company A and ended at
    company B. Company A kept its "resolved" mark and zero contacts, so it
    was skipped forever as "already resolved". This is not rare: a recruiter
    at a staffing agency appears at 2-3 companies.

    Rule: verified data is worth more than fresh data, and the first link is
    never overwritten.

    The first link stays in `person.company_id`, and all other links go to
    `person_company`, where the cache step of the contact search reads them.
    So a person who really works for two companies is no longer lost for the
    second one.

    What did NOT change: a person is still defined only by contact keys.
    A company is not a key and never will be. Two HR people at one company
    are two people, not one (see
    test_two_different_people_are_never_merged_by_a_shared_company).
    """
    keys = identity_keys(contacts)
    pid = find_person(conn, keys)
    if pid is None:
        cur = conn.execute(
            "INSERT INTO person(name, role, company_id) VALUES(?,?,?)",
            (name, role, company_id),
        )
        pid = cur.lastrowid
    else:
        conn.execute(
            "UPDATE person SET name=COALESCE(?, name), role=COALESCE(?, role), "
            "company_id=COALESCE(company_id, ?) WHERE id=?",
            (name, role, company_id, pid),
        )
    if company_id is not None:
        # The full "person -> companies" picture. INSERT OR IGNORE: the
        # contact search calls this function on EVERY find, and a second
        # meeting of the same pair must neither duplicate the row nor
        # overwrite the first-seen mark. Same rule "verified beats fresh"
        # as for identity.value_raw.
        #
        # company_id=None does not get here on purpose: it means "company
        # unknown" (this is how the pipeline creates a person from a contact
        # in the post text), not a reason to invent a link.
        conn.execute(
            "INSERT OR IGNORE INTO person_company(person_id, company_id, first_seen_at) "
            "VALUES(?,?,?)",
            (pid, company_id, datetime.now(timezone.utc).isoformat()),
        )
    # The contact "as it appeared", by key (kind, value_norm). identity_keys
    # collapses duplicates by value_norm and loses which Contact had which
    # spelling. We take the first match from the original list. The order
    # inside contacts is stable (it is the order of regex matches in the text).
    raw_by_key = {}
    for c in contacts:
        raw_by_key.setdefault((c.kind, c.value_norm), c.value)
    for kind, value in keys:
        value_raw = raw_by_key.get((kind, value)) or None
        # INSERT OR IGNORE creates a new row (with value_raw right away) and
        # does not touch an existing one. The UPDATE below handles that.
        conn.execute(
            "INSERT OR IGNORE INTO identity(kind, value_norm, person_id, value_raw) "
            "VALUES(?,?,?,?)",
            (kind, value, pid, value_raw),
        )
        if value_raw:
            # Backfill: the row already existed (created before this change,
            # or an earlier step did not pass the spelling) and its value_raw
            # is empty. The first VERIFIED spelling is never overwritten. Same
            # rule as for the company in the UPDATE above: verified beats fresh.
            conn.execute(
                "UPDATE identity SET value_raw=? WHERE kind=? AND value_norm=? "
                "AND (value_raw IS NULL OR value_raw='')",
                (value_raw, kind, value),
            )
    conn.commit()
    return pid
