"""Love-note moderation previews: the note text staff see is redacted, always.

A love note reaches moderators as ``moderation_body`` -- the privacy-safe rendering
of the body produced once by Azure AI Language PII detection when the note is sent.
The original ``body`` stays exactly as the sender wrote it (the recipient's copy and
the rasterised image are built from it) and is never handed to any admin-facing
endpoint. A redaction failure stores *nothing*: the moderator sees an explicit
"preview unavailable" state, never the raw note.

These tests pin the rules, not the vendor:

* the SDK's own ``redacted_text`` is the single-pass answer, used verbatim;
* the two-pass Malayalam fallback (romanized Malayalam is detected as English, so
  both passes run on Latin-script notes) merges the union of detected spans;
* every failure -- detection, any pass, missing config, empty/over-long text -- is
  fail-closed: no preview is stored and no admin payload gains a ``body`` key;
* the receiver still gets the original, unmodified body, and the retry endpoint can
  refill a missing preview without ever returning the raw note.

Azure never runs for real here: ``get_pii_client`` is replaced with a fake that
speaks the SDK's result shapes, and the real ``redact_love_note_body`` (saved before
the conftest fixture replaces it) is re-installed for the tests that exercise it.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from app.accounts import AccountType
from app.config import settings
from app.db_models import LoveNoteRecord, UserRecord
from app.main import app
from app.models import UserDetails
from app.services import love_note_service as lns
from app.services import pii_detection_service as pds
from app.services.auth_service import get_current_user
from app.services.storage_service import storage_service

# Saved before any fixture runs: the conftest autouse fixture replaces the module
# attributes, and these tests put the real implementation back.
_REAL_REDACT = pds.redact_love_note_body

MALAYALAM_NUMBER = "എന്റെ നമ്പർ 9876543210 ആണ്"


def detect_ok(iso, *, name=None, confidence=1.0):
    return SimpleNamespace(
        is_error=False,
        primary_language=SimpleNamespace(
            name=name or iso, iso6391_name=iso, confidence_score=confidence
        ),
    )


def detect_error(code):
    return SimpleNamespace(is_error=True, error=SimpleNamespace(code=code))


def pii_result(redacted_text, spans=()):
    return SimpleNamespace(
        is_error=False,
        redacted_text=redacted_text,
        entities=[
            SimpleNamespace(
                category=category,
                offset=offset,
                length=length,
                text=redacted_text[offset:offset + length],
            )
            for category, offset, length in spans
        ],
    )


def pii_error(code):
    return SimpleNamespace(is_error=True, error=SimpleNamespace(code=code))


class FakeTextAnalyticsClient:
    """The SDK surface ``pii_detection_service`` uses, in the SDK's result shapes.

    ``detection`` is the single language result (or a DocumentError). ``pii`` maps a
    language code to a result, a DocumentError, or an exception to raise, so a test
    can make one pass fail while the other succeeds. Every call is recorded with its
    arguments, which is how the pass selection is asserted.
    """

    def __init__(self, detection=None, pii=None):
        self.detection = detection if detection is not None else detect_ok("en")
        self.pii = dict(pii or {})
        self.calls = []
        self.closed = False

    def detect_language(self, documents, **kwargs):
        self.calls.append(("detect_language", list(documents), kwargs))
        if isinstance(self.detection, Exception):
            raise self.detection
        return [self.detection]

    def recognize_pii_entities(self, documents, **kwargs):
        self.calls.append(("recognize_pii_entities", list(documents), kwargs))
        entry = self.pii.get(kwargs.get("language"))
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            raise AssertionError(f"unexpected PII pass for {kwargs.get('language')}")
        return [entry]

    def close(self):
        self.closed = True

    def pii_languages(self):
        return [
            kwargs["language"]
            for operation, _documents, kwargs in self.calls
            if operation == "recognize_pii_entities"
        ]


def use_real_redaction(monkeypatch, client):
    """Restore the real implementation and point its client factory at ``client``."""
    monkeypatch.setattr(pds, "get_pii_client", lambda config: client)
    monkeypatch.setattr(pds, "redact_love_note_body", _REAL_REDACT)
    monkeypatch.setattr(lns, "redact_love_note_body", _REAL_REDACT)
    return client


# ------------------------------------------------------------------ pass selection


def test_a_single_pass_is_the_provider_redaction_used_verbatim(monkeypatch):
    """The SDK's ``redacted_text`` is the answer; nothing re-implements the marker.

    Malayalam-script text is detected as ``ml`` and carries no Latin letters, so it
    is exactly one pass. The fake provider marks its own output unambiguously; the
    preview must be that string byte for byte, not a recomputation from spans.
    """
    provider_text = "എന്റെ നമ്പർ [സ്വകാര്യം] ആണ്"
    client = FakeTextAnalyticsClient(
        detection=detect_ok("ml"),
        pii={"ml": pii_result(provider_text, spans=[("PhoneNumber", 12, 10)])},
    )
    use_real_redaction(monkeypatch, client)

    result = _REAL_REDACT(MALAYALAM_NUMBER)

    assert result.moderation_body == provider_text
    assert result.languages == ("ml",)
    assert client.pii_languages() == ["ml"]
    assert client.closed, "the per-call client must be closed"


def test_malayalam_script_notes_use_the_malayalam_pass(monkeypatch):
    client = FakeTextAnalyticsClient(
        detection=detect_ok("ml", confidence=0.99),
        pii={"ml": pii_result("എന്റെ നമ്പർ ********** ആണ്", spans=[("PhoneNumber", 12, 10)])},
    )
    use_real_redaction(monkeypatch, client)

    result = _REAL_REDACT(MALAYALAM_NUMBER)

    assert client.pii_languages() == ["ml"]
    assert "9876543210" not in result.moderation_body


def test_latin_script_notes_run_both_passes_and_merge_found_spans(monkeypatch):
    """The bounded Manglish fallback: each pass finds what the other missed.

    Romanized Malayalam is classified as English with high confidence by the
    service, so the English pass alone would miss Malayalam-trained entities and the
    Malayalam pass alone would miss English ones. The merged preview removes both.
    """
    text = "John gave me 9876543210"
    client = FakeTextAnalyticsClient(
        detection=detect_ok("en"),
        pii={
            "en": pii_result("**** gave me 9876543210", spans=[("Person", 0, 4)]),
            "ml": pii_result("John gave me **********", spans=[("PhoneNumber", 13, 10)]),
        },
    )
    use_real_redaction(monkeypatch, client)

    result = _REAL_REDACT(text)

    assert client.pii_languages() == ["en", "ml"], "bounded to the two supported passes"
    assert result.moderation_body == "**** gave me **********"
    assert result.languages == ("en", "ml")
    assert result.entity_count == 2


def test_manglish_classified_as_malayalam_still_runs_both_passes(monkeypatch):
    """Live verification found the service can call romanized Malayalam ``ml``.

    The fallback keys off the script, not the detected code: a note containing
    Latin letters gets both models whichever way detection swings, so a name
    only the English model sees and a number only the Malayalam model sees are
    both redacted.
    """
    text = "Ente peru John Smith aanu, ente number 9876543210 aanu"
    name_at = text.index("John Smith")
    phone_at = text.index("9876543210")
    client = FakeTextAnalyticsClient(
        detection=detect_ok("ml"),
        pii={
            "ml": pii_result(
                "Ente peru John Smith aanu, ente number ********** aanu",
                spans=[("PhoneNumber", phone_at, 10)],
            ),
            "en": pii_result(
                "Ente peru ********** aanu, ente number 9876543210 aanu",
                spans=[("Person", name_at, 10)],
            ),
        },
    )
    use_real_redaction(monkeypatch, client)

    result = _REAL_REDACT(text)

    assert client.pii_languages() == ["ml", "en"], "the primary pass runs first"
    assert result.moderation_body == "Ente peru ********** aanu, ente number ********** aanu"
    assert result.entity_count == 2


def test_overlapping_spans_from_the_two_passes_redact_once(monkeypatch):
    text = "email me at john@example.com today"
    client = FakeTextAnalyticsClient(
        detection=detect_ok("en"),
        pii={
            "en": pii_result(text, spans=[("Email", 12, 16)]),
            # The Malayalam pass sees the same address as a slightly wider span.
            "ml": pii_result(text, spans=[("Email", 12, 16), ("Person", 0, 5)]),
        },
    )
    use_real_redaction(monkeypatch, client)

    result = _REAL_REDACT(text)

    assert result.moderation_body == "***** me at **************** today"
    assert result.entity_count == 2


def test_a_span_outside_the_text_cannot_extend_the_preview(monkeypatch):
    """A provider bug is dropped, not applied: the body is the only coordinate space."""
    text = "hello there"
    client = FakeTextAnalyticsClient(
        detection=detect_ok("en"),
        pii={
            "en": pii_result(text, spans=[("Person", 0, 5)]),
            "ml": pii_result(text, spans=[("PhoneNumber", 0, 500)]),
        },
    )
    use_real_redaction(monkeypatch, client)

    result = _REAL_REDACT(text)

    assert result.moderation_body == "***** there"


def test_a_language_outside_the_supported_pair_resolves_by_script(monkeypatch):
    client = FakeTextAnalyticsClient(
        detection=detect_ok("fr"),
        pii={
            "en": pii_result("**** bonjour", spans=[("Person", 0, 4)]),
            "ml": pii_result("Jean bonjour", spans=[]),
        },
    )
    use_real_redaction(monkeypatch, client)

    result = _REAL_REDACT("Jean bonjour")

    assert result.languages == ("en", "ml")
    assert result.moderation_body == "**** bonjour"


# --------------------------------------------------------------------- fail closed


def test_detection_failure_raises_and_stores_nothing(monkeypatch):
    client = FakeTextAnalyticsClient(detection=detect_error("UnsupportedLanguageCode"))
    use_real_redaction(monkeypatch, client)

    with pytest.raises(pds.PiiDetectionError) as excinfo:
        _REAL_REDACT("some note")

    assert excinfo.value.code == "language_unavailable"
    assert client.pii_languages() == [], "no PII pass may run on an undetected language"


def test_any_failing_pass_fails_the_whole_redaction(monkeypatch):
    """A pass that did not run cannot be assumed to have found nothing."""
    client = FakeTextAnalyticsClient(
        detection=detect_ok("en"),
        pii={
            "en": pii_result("**** note", spans=[("Person", 0, 4)]),
            "ml": pii_error("InvalidDocument"),
        },
    )
    use_real_redaction(monkeypatch, client)

    with pytest.raises(pds.PiiDetectionError) as excinfo:
        _REAL_REDACT("John note")

    assert excinfo.value.code == "pii_unavailable"


def test_a_transport_failure_is_mapped_without_provider_bodies(monkeypatch):
    client = FakeTextAnalyticsClient(detection=RuntimeError("boom"))
    use_real_redaction(monkeypatch, client)

    with pytest.raises(pds.PiiDetectionError) as excinfo:
        _REAL_REDACT("a note")

    assert excinfo.value.code == "model_error"


def test_empty_text_never_reaches_the_provider(monkeypatch):
    built = []

    def _factory(config):
        built.append(True)
        return FakeTextAnalyticsClient()

    monkeypatch.setattr(pds, "get_pii_client", _factory)
    monkeypatch.setattr(pds, "redact_love_note_body", _REAL_REDACT)

    with pytest.raises(pds.PiiDetectionError) as excinfo:
        _REAL_REDACT("   \n  ")

    assert excinfo.value.code == "invalid_input"
    assert built == [], "no client and no call for text that cannot be redacted"


def test_overlong_text_is_refused_before_any_call(monkeypatch):
    built = []

    def _factory(config):
        built.append(True)
        return FakeTextAnalyticsClient()

    monkeypatch.setattr(pds, "get_pii_client", _factory)
    monkeypatch.setattr(pds, "redact_love_note_body", _REAL_REDACT)

    with pytest.raises(pds.PiiDetectionError) as excinfo:
        _REAL_REDACT("x" * (pds.TEXT_MAX_LENGTH + 1))

    assert excinfo.value.code == "invalid_input"
    assert built == []


def test_missing_configuration_is_an_explicit_error(monkeypatch):
    monkeypatch.setattr(settings, "PII_DETECTION_ENDPOINT", "")
    monkeypatch.setattr(pds, "redact_love_note_body", _REAL_REDACT)

    with pytest.raises(pds.PiiDetectionError) as excinfo:
        _REAL_REDACT("a note")

    assert excinfo.value.code == "missing_config"


# ------------------------------------------------------------------- the send path


def png_base64():
    buf = BytesIO()
    Image.new("RGB", (32, 16), (200, 40, 40)).save(buf, format="PNG")
    import base64

    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def make_user(db, regno, name=None):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name or f"User {regno}",
            email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno}


@pytest.fixture
def login_as(db):
    """Run the statements inside the block as a named participant."""

    @contextmanager
    def _login(regno: str):
        with db.sessions() as session:
            row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        assert row is not None, f"no participant named {regno} to sign in as"
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: UserDetails(
            id=str(row.id), regno=row.regno, name=row.name, email=row.email,
            year=row.year, department=row.department, gender=row.gender,
            matchmaking_enabled=row.matchmaking_enabled,
            notifications_enabled=row.notifications_enabled,
            account_type=AccountType.USERS.value,
        )
        try:
            yield
        finally:
            app.dependency_overrides[get_current_user] = previous

    return _login


@pytest.fixture
def stub_upload(monkeypatch):
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.upload_love_note_image",
        lambda *, image_base64, love_note_id: {
            "public_id": f"love_notes/{love_note_id}/image",
            "format": "png",
        },
        raising=True,
    )


def send_note(client, login_as, recipient, body):
    with login_as("sender"):
        return client.post(
            "/love-notes/send",
            json={
                "recipient_id": recipient["id"],
                "image_base64": png_base64(),
                "body": body,
                "anonymous": False,
            },
        )


ENGLISH_PII_BODY = "Call John Smith on 9876543210"

# One Person span covers "John Smith" -- the inner space is inside the span, so it
# is redacted with the rest of the name rather than left standing between two runs.
ENGLISH_PII_PREVIEW = "Call ********** on **********"


@pytest.fixture
def english_pii_client(monkeypatch):
    client = FakeTextAnalyticsClient(
        detection=detect_ok("en"),
        pii={
            "en": pii_result(
                ENGLISH_PII_PREVIEW,
                spans=[("Person", 5, 10), ("PhoneNumber", 19, 10)],
            ),
            "ml": pii_result(ENGLISH_PII_BODY, spans=[]),
        },
    )
    return use_real_redaction(monkeypatch, client)


def test_sending_stores_the_redacted_preview_never_changing_the_body(
    english_pii_client, stub_upload, client, login_as, db
):
    """Persistence happens once, at creation, and the stored body is untouched."""
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")

    response = send_note(client, login_as, recipient, ENGLISH_PII_BODY)
    assert response.status_code == 201, response.text
    note_id = response.json()["note_id"]

    with db.sessions() as session:
        note = session.get(LoveNoteRecord, note_id)
        assert note.moderation_body == ENGLISH_PII_PREVIEW
        assert note.body == ENGLISH_PII_BODY, "redaction must not rewrite the delivered body"


def test_the_moderation_list_carries_only_the_redacted_preview(
    english_pii_client, stub_upload, client, admin_client, login_as, db
):
    """The raw PII has no path out of any admin response -- not even as a field name."""
    make_user(db, "sender")
    recipient = make_user(db, "recipient")
    assert send_note(client, login_as, recipient, ENGLISH_PII_BODY).status_code == 201

    response = admin_client.get("/admin/love-notes")
    assert response.status_code == 200, response.text
    note = response.json()["notes"][0]

    assert note["moderation_body"] == ENGLISH_PII_PREVIEW
    assert "body" not in note
    assert "9876543210" not in response.text
    assert "John Smith" not in response.text


def test_a_failed_redaction_still_sends_the_note_with_no_preview(
    monkeypatch, stub_upload, client, admin_client, login_as, db
):
    """The sender is not held hostage to the preview provider; staff see unavailable."""
    failing = FakeTextAnalyticsClient(detection=RuntimeError("provider down"))
    use_real_redaction(monkeypatch, failing)
    make_user(db, "sender")
    recipient = make_user(db, "recipient")

    response = send_note(client, login_as, recipient, ENGLISH_PII_BODY)
    assert response.status_code == 201, response.text
    note_id = response.json()["note_id"]

    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).moderation_body is None

    note = admin_client.get("/admin/love-notes").json()["notes"][0]
    assert note["moderation_body"] is None
    assert "body" not in note


def test_the_receiver_still_gets_the_original_body(
    monkeypatch, stub_upload, client, admin_client, login_as, db
):
    """Redaction protects the moderation screen, not the delivery: the recipient reads
    exactly what the sender wrote, PII included."""
    client_azure = FakeTextAnalyticsClient(
        detection=detect_ok("en"),
        pii={
            "en": pii_result(ENGLISH_PII_PREVIEW, spans=[("Person", 5, 10)]),
            "ml": pii_result(ENGLISH_PII_BODY, spans=[]),
        },
    )
    use_real_redaction(monkeypatch, client_azure)
    make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = send_note(client, login_as, recipient, ENGLISH_PII_BODY).json()["note_id"]
    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "approved"})

    with login_as("recipient"):
        inbox = client.get("/love-notes/inbox")

    assert inbox.status_code == 200, inbox.text
    inbox_note = inbox.json()["notes"][0]
    assert inbox_note["body"] == ENGLISH_PII_BODY
    assert "moderation_body" not in inbox_note


def test_the_reports_queue_carries_only_the_preview(
    english_pii_client, stub_upload, client, admin_client, login_as, db
):
    make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = send_note(client, login_as, recipient, ENGLISH_PII_BODY).json()["note_id"]
    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "approved"})
    with login_as("recipient"):
        filed = client.post(
            f"/love-notes/inbox/{note_id}/report", json={"reason": "harassment"}
        )
    assert filed.status_code == 201, filed.text

    response = admin_client.get("/admin/love-notes/reports")
    report = response.json()["reports"][0]

    assert report["moderation_body"] == ENGLISH_PII_PREVIEW
    assert "body" not in report
    assert "9876543210" not in response.text
    assert "John Smith" not in response.text


def test_report_search_matches_the_preview_not_the_raw_body(
    english_pii_client, stub_upload, client, admin_client, login_as, db
):
    """The search is an oracle: matching the raw body would reveal a redacted value."""
    make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = send_note(client, login_as, recipient, ENGLISH_PII_BODY).json()["note_id"]
    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "approved"})
    with login_as("recipient"):
        client.post(f"/love-notes/inbox/{note_id}/report", json={"reason": "harassment"})

    visible = admin_client.get("/admin/love-notes/reports", params={"q": "call"}).json()
    withheld = admin_client.get("/admin/love-notes/reports", params={"q": "9876543210"}).json()

    assert visible["total"] == 1
    assert withheld["total"] == 0, "search must not confirm a value the preview hides"


# ---------------------------------------------------------------- the retry endpoint


def insert_note(db, sender, recipient, *, body="a note", moderation_body=None):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            id=str(uuid4()),
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id=None,
            image_format="png",
            body=body,
            moderation_body=moderation_body,
            anonymous=False,
            status="pending_review",
            created_at=datetime.now(timezone.utc),
        )
        row.image_public_id = storage_service.love_note_public_id(str(row.id))
        session.add(row)
        session.flush()
        return str(row.id)


def test_retry_regenerates_a_missing_preview_and_returns_only_the_preview(
    monkeypatch, admin_client, db
):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = insert_note(db, sender, recipient, body=ENGLISH_PII_BODY)
    client_azure = FakeTextAnalyticsClient(
        detection=detect_ok("en"),
        pii={
            "en": pii_result(
                ENGLISH_PII_PREVIEW,
                spans=[("Person", 5, 10), ("PhoneNumber", 19, 10)],
            ),
            "ml": pii_result(ENGLISH_PII_BODY, spans=[]),
        },
    )
    use_real_redaction(monkeypatch, client_azure)

    response = admin_client.post(f"/admin/love-notes/{note_id}/redact")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {"id": note_id, "moderation_body": ENGLISH_PII_PREVIEW}
    assert "9876543210" not in response.text
    with db.sessions() as session:
        note = session.get(LoveNoteRecord, note_id)
        assert note.moderation_body == ENGLISH_PII_PREVIEW
        assert note.body == ENGLISH_PII_BODY


def test_retry_is_a_no_op_when_a_preview_exists(monkeypatch, admin_client, db):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = insert_note(
        db, sender, recipient, body=ENGLISH_PII_BODY, moderation_body="already **** *****"
    )
    client_azure = FakeTextAnalyticsClient(
        detection=RuntimeError("must never be called for an existing preview")
    )
    use_real_redaction(monkeypatch, client_azure)

    response = admin_client.post(f"/admin/love-notes/{note_id}/redact")

    assert response.status_code == 200, response.text
    assert response.json()["moderation_body"] == "already **** *****"
    assert client_azure.calls == []


def test_retry_refuses_with_a_generic_503_when_the_provider_fails(
    monkeypatch, admin_client, db
):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = insert_note(db, sender, recipient, body=ENGLISH_PII_BODY)
    use_real_redaction(monkeypatch, FakeTextAnalyticsClient(detection=RuntimeError("down")))

    response = admin_client.post(f"/admin/love-notes/{note_id}/redact")

    assert response.status_code == 503, response.text
    assert "9876543210" not in response.text
    assert "John Smith" not in response.text
    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).moderation_body is None


def test_retry_404s_and_400s_like_every_other_note_action(monkeypatch, admin_client, db):
    def _forbidden(config):
        raise AssertionError("no provider call for a missing or invalid id")

    monkeypatch.setattr(pds, "get_pii_client", _forbidden)
    missing = admin_client.post(
        "/admin/love-notes/11111111-1111-1111-1111-111111111111/redact"
    )
    malformed = admin_client.post("/admin/love-notes/not-an-id/redact")

    assert missing.status_code == 404
    assert malformed.status_code == 400


def test_a_plain_user_cannot_trigger_redaction(client, db):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = insert_note(db, sender, recipient)

    assert client.post(f"/admin/love-notes/{note_id}/redact").status_code == 403
