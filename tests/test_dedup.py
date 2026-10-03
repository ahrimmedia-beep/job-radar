import pytest

from jobradar.db import connect, init_schema
from jobradar.dedup import content_key, identity_keys, mark_seen, upsert_person
from jobradar.models import Contact


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_schema(c)
    return c


def _company(conn, name):
    """A plain company row. The company registry module is not part of this repository."""
    cur = conn.execute("INSERT INTO company(name) VALUES(?)", (name,))
    conn.commit()
    return cur.lastrowid


def test_key_order_is_stable_within_same_kind():
    """The order must not depend on PYTHONHASHSEED, or merging is not deterministic."""
    cs = [Contact("telegram", "@zzz_hr", "zzz_hr"),
          Contact("telegram", "@aaa_hr", "aaa_hr")]
    keys = identity_keys(cs)
    assert [k[1] for k in keys] == ["aaa_hr", "zzz_hr"], \
        f"order inside one kind of key is not stable: {keys}"


def test_linkedin_is_strongest_key():
    cs = [Contact("telegram", "@x", "x"), Contact("linkedin", "l", "jane-doe")]
    assert identity_keys(cs)[0] == ("linkedin", "jane-doe")


def test_role_mailbox_is_not_a_key():
    cs = [Contact("email", "hr@example.com", "hr@example.com")]
    assert identity_keys(cs) == []


def test_personal_email_is_a_key():
    cs = [Contact("email", "jane@example.com", "jane@example.com")]
    assert ("email", "jane@example.com") in identity_keys(cs)


def test_same_person_from_two_sources_merges(conn):
    tg = [Contact("telegram", "@jane_hr", "jane_hr")]
    both = [Contact("telegram", "@jane_hr", "jane_hr"),
            Contact("linkedin", "l", "jane-doe")]
    first = upsert_person(conn, "Jane", "HR", tg, None)
    second = upsert_person(conn, "Jane Doe", "Recruiter", both, None)
    assert first == second, "one person must not split in two"


def test_different_people_stay_separate(conn):
    a = upsert_person(conn, "A", "HR", [Contact("telegram", "@aaa_hr", "aaa_hr")], None)
    b = upsert_person(conn, "B", "HR", [Contact("telegram", "@bbb_hr", "bbb_hr")], None)
    assert a != b


def test_name_and_company_alone_do_not_merge(conn):
    a = upsert_person(conn, "John Smith", "CTO", [], None)
    b = upsert_person(conn, "John Smith", "CTO", [], None)
    assert a != b, "a full name cannot be a key: a false merge is worse than a duplicate"


def test_mark_seen_counts_distinct_days_not_runs(conn):
    """The counter is the number of DIFFERENT DAYS a post was seen, not the number of calls.

    Before, mark_seen counted by ID, which equals the number of script runs:
    two runs in one day already meant "reposted twice", though it was the
    same day. A content fingerprint plus the day fix both halves of the bug.
    """
    k = content_key("tg", "Hiring a Python developer for our team")
    assert mark_seen(conn, k, day="2026-08-20") == 1
    assert mark_seen(conn, k, day="2026-08-20") == 1, \
        "a second call on the same day must not inflate the counter"
    assert mark_seen(conn, k, day="2026-08-21") == 2
    assert mark_seen(conn, k, day="2026-08-22") == 3


def test_content_key_ignores_links_and_punctuation(conn):
    """A repost of the same vacancy under a new ID gives the same fingerprint."""
    a = content_key("telegram", "Hiring a Python-developer! https://t.me/hr_a/1")
    b = content_key("telegram", "hiring a python developer https://t.me/hr_b/999")
    assert a == b


# ------------------------------------------------- a person seen at a second company


def test_second_company_does_not_steal_a_person_from_the_first(conn):
    """`company_id=COALESCE(?, company_id)` used to overwrite the company that
    was already set, and the person silently moved to the last company that
    named them.

    `identity` is unique globally ((kind, value_norm) is the primary key), so
    one contact is exactly one `person`. A recruiter at a staffing agency, or
    a person who runs two brands, appears at 2-3 companies. This is normal
    input, not an edge case.

    What broke on real data: a person started at company A and ended at
    company B. Company A kept its "resolved" mark and ZERO contacts, so it
    was skipped forever as "already resolved", and the owner read
    "resolved N" and found nobody.

    The rule now: verified data is worth more than fresh data, and the first
    link is never overwritten.
    """
    acme = _company(conn, "Acme")
    globex = _company(conn, "Globex")
    contacts = [Contact("telegram", "@dual_hr", "dual_hr")]
    first = upsert_person(conn, None, None, contacts, acme)
    second = upsert_person(conn, None, None, contacts, globex)

    assert first == second, "the same contact must stay the same person"
    company_id = conn.execute(
        "SELECT company_id FROM person WHERE id=?", (first,)).fetchone()[0]
    assert company_id == acme, (
        f"the person was silently moved from Acme ({acme}) to Globex ({globex}): "
        f"company_id={company_id}")


def test_company_is_filled_in_when_the_person_had_none(conn):
    """The other side: an empty link CAN and MUST be filled in.

    This is how the pipeline works: it creates a person from a contact in the
    post text with `company_id=None` (the company of the post is not parsed
    yet), and the contact search later finds the same person at a specific
    company. If "first record" meant "first value, NULL included", the cache
    step could not return a SINGLE person, and the cost model of the whole
    contact search depends on that step.
    """
    initech = _company(conn, "Initech")
    contacts = [Contact("telegram", "@late_hr", "late_hr")]
    pid = upsert_person(conn, None, None, contacts, None)
    same = upsert_person(conn, None, None, contacts, initech)

    assert pid == same
    company_id = conn.execute(
        "SELECT company_id FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert company_id == initech, (
        f"the company was not set for a person who had none: {company_id}")


def test_name_and_role_still_fill_in_but_never_overwrite(conn):
    """Name and role behave as before: an empty value is filled in, a filled one is kept.

    This check sits next to the company fix on purpose: the change touched
    ONE column in a shared UPDATE, and the fields next to it were easy to
    break. A step that knows people by name creates the person with a name,
    while the contact search calls `upsert_person(name=None)`. Overwriting
    with `None` would erase a name that is already known.
    """
    contacts = [Contact("telegram", "@named_hr", "named_hr")]
    pid = upsert_person(conn, "Jane", "HR", contacts, None)
    upsert_person(conn, None, None, contacts, None)
    row = conn.execute(
        "SELECT name, role FROM person WHERE id=?", (pid,)).fetchone()
    assert (row[0], row[1]) == ("Jane", "HR"), \
        f"a call with None erased the known name/role: {tuple(row)}"

    upsert_person(conn, "Jane Doe", "Recruiter", contacts, None)
    row = conn.execute(
        "SELECT name, role FROM person WHERE id=?", (pid,)).fetchone()
    assert (row[0], row[1]) == ("Jane Doe", "Recruiter"), \
        f"fresh name/role must be written: {tuple(row)}"


# ------------------------------------------------- original casing of LinkedIn links
#
# `value_norm` (lower case) is the dedup key. It is harmless for telegram and
# email, but some LinkedIn profile IDs are case sensitive: the lower case form
# of such an ID is another link that does not exist. `value_raw` keeps the
# contact as it appeared, so the owner can really open the link.

_LI_RAW = "https://www.linkedin.com/in/ACwAAExampleProfileId_MixedCase01"
_LI_NORM = "acwaaexampleprofileid_mixedcase01"


def test_value_raw_stores_original_casing(conn):
    """A contact whose spelling differs from value_norm in case must reach
    identity.value_raw unchanged. Otherwise the LinkedIn link points to a
    page that does not exist."""
    contacts = [Contact("linkedin", _LI_RAW, _LI_NORM)]
    upsert_person(conn, "Jane", "HR", contacts, None)

    row = conn.execute(
        "SELECT value_raw FROM identity WHERE kind='linkedin' AND value_norm=?",
        (_LI_NORM,),
    ).fetchone()
    assert row[0] == _LI_RAW, \
        f"original spelling lost: stored {row[0]!r}, expected {_LI_RAW!r}"


def test_value_raw_is_backfilled_when_missing(conn):
    """The row already exists (for example, created before this change, with an
    empty value_raw), and the next call brings the real spelling: the empty
    field must be filled in, not stay NULL forever."""
    contacts = [Contact("linkedin", _LI_NORM, _LI_NORM)]  # first record "without the raw spelling"
    upsert_person(conn, "Jane", "HR", contacts, None)
    # Simulate a production row created before the change: value_raw is empty.
    conn.execute(
        "UPDATE identity SET value_raw=NULL WHERE kind='linkedin' AND value_norm=?",
        (_LI_NORM,),
    )
    conn.commit()

    upsert_person(conn, "Jane", "HR", [Contact("linkedin", _LI_RAW, _LI_NORM)], None)

    row = conn.execute(
        "SELECT value_raw FROM identity WHERE kind='linkedin' AND value_norm=?",
        (_LI_NORM,),
    ).fetchone()
    assert row[0] == _LI_RAW, \
        f"empty value_raw was not filled with the fresh spelling: {row[0]!r}"


def test_value_raw_already_filled_is_not_overwritten(conn):
    """The first value_raw is verified (the first spelling is checked, a fresh one
    may come from a less reliable step). A second call with a DIFFERENT spelling
    of the same key must not overwrite it."""
    contacts_first = [Contact("linkedin", _LI_RAW, _LI_NORM)]
    upsert_person(conn, "Jane", "HR", contacts_first, None)

    worse_raw = _LI_RAW.lower()  # another step brought a less reliable spelling
    upsert_person(conn, "Jane", "HR", [Contact("linkedin", worse_raw, _LI_NORM)], None)

    row = conn.execute(
        "SELECT value_raw FROM identity WHERE kind='linkedin' AND value_norm=?",
        (_LI_NORM,),
    ).fetchone()
    assert row[0] == _LI_RAW, \
        f"an already filled value_raw was overwritten by a fresh call: {row[0]!r}"


def test_dedup_still_keyed_by_value_norm_not_value_raw(conn):
    """People are merged by value_norm: the same contact seen with a different
    letter case must not create a second person."""
    first = upsert_person(
        conn, "Jane", "HR", [Contact("linkedin", _LI_RAW, _LI_NORM)], None)
    second = upsert_person(
        conn, "Jane", "HR", [Contact("linkedin", _LI_RAW.upper(), _LI_NORM)], None)

    assert first == second, \
        "a different spelling of the same contact split the person in two"
    count = conn.execute(
        "SELECT COUNT(*) FROM identity WHERE kind='linkedin' AND value_norm=?",
        (_LI_NORM,),
    ).fetchone()[0]
    assert count == 1, f"identity must keep one row for the key, but has {count}"


# ------------------------------------------------- one person, several companies


def _links(conn, pid):
    return {r[0] for r in conn.execute(
        "SELECT company_id FROM person_company WHERE person_id=?", (pid,))}


def test_person_met_at_two_companies_is_linked_to_both(conn):
    """The same recruiter at two companies: BOTH links exist, and there is one person.

    This is the second half of the company fix. The first half (do not take a
    person away from the first company) is tested above and still holds:
    `person.company_id` keeps the first link. But the second company used to
    keep a "resolved" mark and zero contacts, so it was skipped forever as
    "already resolved", and the owner read "resolved N" and found nobody.
    """
    acme = _company(conn, "Acme")
    globex = _company(conn, "Globex")
    contacts = [Contact("telegram", "@dual_hr", "dual_hr")]

    first = upsert_person(conn, None, None, contacts, acme)
    second = upsert_person(conn, None, None, contacts, globex)

    assert first == second, "the same contact must stay the same person"
    assert _links(conn, first) == {acme, globex}, (
        f"the person was seen at two companies, but the links are {_links(conn, first)}: "
        "one of the companies would keep a resolved mark and zero contacts")
    assert conn.execute(
        "SELECT company_id FROM person WHERE id=?", (first,)).fetchone()[0] == acme, \
        "the first link in person.company_id must not change"


def test_link_is_not_invented_for_a_person_without_a_company(conn):
    """company_id=None means "company unknown", not "new link".

    This is how the pipeline calls it: it creates a person from a contact in
    the post text when the company is not parsed yet. A link to an invented
    company would be exactly what the project rule forbids: a merge without
    data.

    The database has another company ON PURPOSE: without it, "no links
    appeared" would only prove that there was nothing to link to (checked by
    mutation: a link to the first company found would pass unnoticed,
    because `INSERT OR IGNORE` also swallows a foreign key violation).
    """
    other = _company(conn, "Other Co")
    pid = upsert_person(conn, None, None,
                        [Contact("telegram", "@nobody", "nobody")], None)
    assert _links(conn, pid) == set(), (
        f"a person without a company got links: {_links(conn, pid)} "
        f"(the only company in the database was another one, {other})")


def test_same_company_twice_does_not_duplicate_the_link(conn):
    """The contact search calls upsert_person on EVERY find, so a repeat must be quiet.

    The first timestamp is not overwritten either: same rule as for
    `identity.value_raw`, verified beats fresh.
    """
    acme = _company(conn, "Acme")
    contacts = [Contact("telegram", "@acme_hr", "acme_hr")]
    pid = upsert_person(conn, None, None, contacts, acme)
    first_seen = conn.execute(
        "SELECT first_seen_at FROM person_company WHERE person_id=?",
        (pid,)).fetchone()[0]
    assert first_seen, "the first link must get a timestamp"

    upsert_person(conn, None, None, contacts, acme)

    rows = conn.execute(
        "SELECT first_seen_at FROM person_company WHERE person_id=?", (pid,)).fetchall()
    assert len(rows) == 1, f"the link was duplicated: {len(rows)} rows"
    assert rows[0][0] == first_seen, \
        f"the first-seen mark was overwritten: was {first_seen}, now {rows[0][0]}"


def test_two_different_people_are_never_merged_by_a_shared_company(conn):
    """The link table does not soften the merge rule: a shared company is not a key.

    "A person can be at several companies" is easy to read as "the company is
    part of a person's identity". That is exactly the mistake that makes the
    owner write "I saw your post" to the wrong person: two HR people at one
    company are two different people, and each has their own key.
    """
    acme = _company(conn, "Acme")
    jane = upsert_person(conn, "Jane", "HR",
                         [Contact("telegram", "@jane_hr", "jane_hr")], acme)
    bob = upsert_person(conn, "Bob", "HR",
                        [Contact("telegram", "@bob_hr", "bob_hr")], acme)

    assert jane != bob, "two different contacts at one company were merged into one person"
    assert _links(conn, jane) == _links(conn, bob) == {acme}
