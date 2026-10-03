"""Contact extraction with regular expressions ONLY.

The model never generates contacts. It can only pick one of the contacts
already found here. verify_contact is the last line of defense: if the
string is not in the source text word for word, the contact is dropped.
"""
import re

from .models import Contact

# Role mailboxes: behind them is a queue, not a person. They cannot be a dedup key.
ROLE_MAILBOXES = frozenset({
    "hr", "jobs", "job", "career", "careers", "info", "contact", "contacts",
    "hello", "team", "support", "recruiting", "recruitment", "talent", "cv",
    # Added after a live run: a vacancy page put the job board's service
    # mailbox from the page footer (abuse@...) into a card. The owner would
    # have written to nobody and decided that the system invents contacts.
    "abuse", "noreply", "no-reply", "donotreply", "admin", "webmaster",
    "postmaster", "help", "office", "sales", "marketing", "press", "legal",
    "privacy", "billing", "notifications", "mail", "email",
})

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# Retina assets look like an email up to the last dot: a live scrape of a
# company website found `awards@2x.webp`. The website step sends whole pages
# through this module, and such names would end up in the people table.
# `verify_contact` would let them through, because they really are in the
# source text word for word.
_ASSET_SUFFIX = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg|ico|bmp|avif|css|js|woff2?|ttf|eot|mp4|webm)$", re.I)
_TME = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})\b", re.I)
_HANDLE = re.compile(r"(?<![\w@/#])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
_LINKEDIN = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]{3,100})", re.I)
_PHONE = re.compile(r"\+971[\s\-()]?\d[\d\s\-()]{6,12}\d")
# `\b` on both sides is required: without it `line` swallows a valid handle
# in text like "Deadline @example_hr". Checked on 14 cases.
_OTHER_PLATFORM = re.compile(
    r"\b(whats\s?app|wa\.me|wechat|weixin|viber|skype|instagram|insta|signal|line)\b"
    r"[\s:：/\-–—]*$", re.I)


def _norm_phone(raw: str) -> str:
    return "+" + re.sub(r"\D", "", raw)


def extract_contacts(text: str) -> list[Contact]:
    """Returns contacts in order from the most reliable key to the least."""
    out: list[Contact] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, norm: str) -> None:
        key = (kind, norm)
        if norm and key not in seen:
            seen.add(key)
            out.append(Contact(kind, value, norm))

    for m in _LINKEDIN.finditer(text):
        add("linkedin", m.group(0), m.group(1).lower())
    for m in _EMAIL.finditer(text):
        if _ASSET_SUFFIX.search(m.group(1)):
            continue
        add("email", m.group(1), m.group(1).lower())
    for m in _TME.finditer(text):
        add("telegram", m.group(0), m.group(1).lower())
    for m in _HANDLE.finditer(text):
        # If the name of another messenger stands right before the @,
        # this is not a Telegram handle: writing there means writing to
        # the wrong place.
        if _OTHER_PLATFORM.search(text[max(0, m.start() - 20):m.start()]):
            continue
        add("telegram", m.group(0), m.group(1).lower())
    for m in _PHONE.finditer(text):
        add("phone", m.group(0), _norm_phone(m.group(0)))
    return out


def verify_contact(c: Contact, raw_text: str) -> bool:
    """A contact MUST appear in the source text word for word.

    No fallback that searches for the normalized value: `alice` as part of
    the word "Malice" does not confirm the handle `@alice`. Every contact is
    born in extract_contacts as a slice of the source text, so a strict check
    is enough, and any leniency here is a hole.
    """
    return c.value.lower() in raw_text.lower()


def is_role_mailbox(c: Contact) -> bool:
    if c.kind != "email":
        return False
    return c.value_norm.split("@", 1)[0] in ROLE_MAILBOXES
