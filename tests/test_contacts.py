from jobradar.contacts import ROLE_MAILBOXES, extract_contacts, verify_contact
from jobradar.models import Contact


def test_extracts_telegram_handle():
    cs = extract_contacts("Send your CV to @example_hr")
    assert Contact("telegram", "@example_hr", "example_hr") in cs


def test_extracts_tme_link_as_telegram():
    cs = extract_contacts("CV here: https://t.me/Sample_Recruiter")
    assert any(c.kind == "telegram" and c.value_norm == "sample_recruiter" for c in cs)


def test_extracts_email_lowercased():
    cs = extract_contacts("send to Jane@Example.com please")
    assert any(c.kind == "email" and c.value_norm == "jane@example.com" for c in cs)


def test_extracts_linkedin_slug():
    cs = extract_contacts("profile https://www.linkedin.com/in/Jane-Doe-123/")
    assert any(c.kind == "linkedin" and c.value_norm == "jane-doe-123" for c in cs)


def test_extracts_uae_phone():
    cs = extract_contacts("WhatsApp +971 50 000 0000")
    assert any(c.kind == "phone" and c.value_norm == "+971500000000" for c in cs)


def test_ignores_hashtags_and_emails_as_handles():
    cs = extract_contacts("#vacancy #python write to hr@example.com")
    handles = [c for c in cs if c.kind == "telegram"]
    assert handles == [], f"hashtags and emails must not become handles: {handles}"


def test_role_mailbox_is_flagged():
    assert "hr" in ROLE_MAILBOXES and "jobs" in ROLE_MAILBOXES


def test_verify_rejects_contact_absent_from_text():
    fake = Contact("telegram", "@ghost_hr", "ghost_hr")
    assert verify_contact(fake, "text without this handle") is False


def test_verify_accepts_contact_present_in_text():
    real = Contact("telegram", "@Sample_Recruiter", "sample_recruiter")
    assert verify_contact(real, "CV: write to @Sample_Recruiter") is True


def test_verify_rejects_substring_collision():
    """@alice is not confirmed by the word "Malice". This is the whole point of the check."""
    fake = Contact("telegram", "@alice", "alice")
    assert verify_contact(fake, "Casting for the role of Malice, filming in Dubai") is False


def test_verify_is_case_insensitive_on_value():
    c = Contact("telegram", "@Sample_Recruiter", "sample_recruiter")
    assert verify_contact(c, "write to @sample_recruiter") is True


def test_platform_word_inside_another_word_does_not_block():
    """"Deadline" must not cut off a valid handle: `line` needs word boundaries."""
    for prefix in ("Deadline", "Timeline:", "Guideline", "Apply online"):
        cs = extract_contacts(f"{prefix} @example_hr, vacancy in Dubai with relocation")
        assert any(c.value_norm == "example_hr" for c in cs), \
            f"handle lost after the word {prefix!r}"


def test_whatsapp_handle_is_not_taken_as_telegram():
    """Format from a real post: signed handles of different messengers. Values replaced."""
    text = "TG：@SampleRecruiter1\nWhatsApp：@SampleRecruiterWA\nWeChat：SampleWeChat"
    norms = {c.value_norm for c in extract_contacts(text) if c.kind == "telegram"}
    assert "samplerecruiter1" in norms
    assert "samplerecruiterwa" not in norms, "a WhatsApp handle must not become a Telegram one"


def test_linkedin_slug_normalised_without_trailing_slash():
    cs = extract_contacts("profile https://www.linkedin.com/in/Jane-Doe-123/")
    assert any(c.kind == "linkedin" and c.value_norm == "jane-doe-123" for c in cs)


def test_image_filenames_are_not_emails():
    """Found on a live scrape of a company website: `awards@2x.webp`.

    Retina assets look like an email up to the last dot. The website step
    parses whole pages, and without this filter file names would pour into
    the people table. `verify_contact` would let them through, because they
    are in the source text word for word.
    """
    junk = "logo awards@2x.webp and sprite@3x.png and icon@2x.svg"
    assert extract_contacts(junk) == []


def test_real_email_next_to_asset_still_extracted():
    """The asset filter must not eat a real email next to the assets."""
    text = "logo@2x.png, write to jane@example.com"
    values = [c.value for c in extract_contacts(text) if c.kind == "email"]
    assert values == ["jane@example.com"]
