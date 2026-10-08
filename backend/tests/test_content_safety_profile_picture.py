"""Azure AI Content Safety for profile pictures.

A profile picture is the one moderated item that is *rejected* rather than
stored hidden: the prepared bytes are analyzed before Cloudinary ever sees
them, a verdict above 1 refuses the upload with a 422, spends the
participant's one change for the window, and notifies staff -- no object, no
report row. Provider failures refuse with a retryable 503 and spend nothing.

The suite runs against real PostgreSQL with the storage layer and Content
Safety mocked, like the other content-safety batches.
"""
import base64
import io
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select, text

from app.accounts import AccountType
from app.config import settings
from app.db_models import ReportRecord, StaffNotificationRecord, UserRecord
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services import content_safety_service as css
from app.services.profile_service import upload_profile_picture_service
from app.services.storage_service import storage_service

# Saved before the conftest safe-mock fixture overwrites them; used by the
# tests that need the real Analyze Image implementation (request shape,
# malformed responses, empty input).
_REAL_ANALYZE_IMAGE = css.analyze_image
_REAL_MODERATE = css.moderate_content
_REAL_MODERATE_PROFILE_PICTURE = css.moderate_profile_picture

# A real 1x1 PNG, so these tests run through the real validator rather than
# around it.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def db():
    return get_postgres_client()


@pytest.fixture(autouse=True)
def live_storage(monkeypatch):
    """Stand in for Cloudinary: record what was stored, destroyed and in what order."""
    uploads: list[str] = []
    destroyed: list[str] = []
    order: list[str] = []

    def _upload(*, content, owner_regno):
        order.append("store")
        public_id = (
            f"profile_pics/{owner_regno}/Stored{len(uploads) + 1:04d}IdUnique/profile"
        )
        uploads.append(public_id)
        return {"public_id": public_id, "format": "png"}

    def _delete(public_id, **kwargs):
        destroyed.append(public_id)
        return True

    monkeypatch.setattr(storage_service, "upload_profile_image", _upload, raising=True)
    monkeypatch.setattr(storage_service, "delete_profile_asset", _delete, raising=True)
    monkeypatch.setattr(
        storage_service,
        "profile_delivery_url",
        lambda *a, **k: "https://cdn.example/x.png",
        raising=True,
    )
    return SimpleNamespace(uploads=uploads, destroyed=destroyed, order=order)


def make_user(db, regno: str = "alice") -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(regno: str = "alice") -> UserDetails:
    return UserDetails(
        regno=regno, name="Test User", email=f"{regno.lower()}@test.com",
        year=2, department="Test_Class", gender="other",
        account_type=AccountType.USERS.value,
    )


def upload_file(content: bytes = ONE_PIXEL_PNG, filename: str = "me.png") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


async def change_picture(db, *, regno: str = "alice", content: bytes = ONE_PIXEL_PNG):
    return await upload_profile_picture_service(as_user(regno), db, upload_file(content))


def last_change_at(db, regno: str):
    with db.sessions() as session:
        return session.scalar(
            select(UserRecord.last_profile_picture_at).where(UserRecord.regno == regno)
        )


def stored_picture(db, regno: str) -> str | None:
    with db.sessions() as session:
        return session.scalar(
            select(UserRecord.profile_picture_public_id).where(UserRecord.regno == regno)
        )


def seed_existing_picture(db, regno: str, public_id: str = "profile_pics/alice/OldIdUnique/profile"):
    with db.sessions.begin() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        row.profile_picture_public_id = public_id
        row.profile_picture_format = "png"


def notifications(db) -> list[StaffNotificationRecord]:
    with db.sessions() as session:
        return list(
            session.scalars(
                select(StaffNotificationRecord).order_by(StaffNotificationRecord.created_at)
            ).all()
        )


def report_count(db) -> int:
    with db.sessions() as session:
        return int(session.scalar(select(func.count()).select_from(ReportRecord)) or 0)


def _mock_picture_scores(monkeypatch, scores: dict, order: list | None = None):
    """Return a chosen severity per category from the central image moderator."""

    def _fake(image, *, config=None):
        if order is not None:
            order.append("moderate")
        return css._decision_for(css.CONTENT_PROFILE_PICTURE, dict(scores))

    monkeypatch.setattr(css, "moderate_profile_picture", _fake)


def _mock_picture_failure(monkeypatch, code: str = "timeout"):
    def _raise(image, *, config=None):
        raise css.ContentSafetyError("provider down", code=code)

    monkeypatch.setattr(css, "moderate_profile_picture", _raise)


def _mock_picture_safe(monkeypatch):
    _mock_picture_scores(
        monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0}
    )


# ------------------------------------------------------------ the policy itself


def test_a_picture_blocks_above_one_and_accounts_no_report():
    """Everything >1 is rejected; 0 and 1 proceed. The strictness is the contract."""
    for maximum in (0, 1):
        assert css.classify_for_content(css.CONTENT_PROFILE_PICTURE, maximum) == css.NO_ACTION
        assert css.should_block_for(css.CONTENT_PROFILE_PICTURE, maximum) is False
    for maximum in (2, 4, 6):
        assert css.classify_for_content(css.CONTENT_PROFILE_PICTURE, maximum) == css.REJECT
        assert css.should_block_for(css.CONTENT_PROFILE_PICTURE, maximum) is True
        assert css.should_report_for(css.CONTENT_PROFILE_PICTURE, maximum) is False
        assert css.should_hide_for(css.CONTENT_PROFILE_PICTURE, maximum) is False


def test_a_blocking_decision_has_no_report_reason():
    """The caller files a staff notification, never a report row, so reason stays empty."""
    decision = css._decision_for(css.CONTENT_PROFILE_PICTURE, {"Hate": 6})
    assert decision.action == css.REJECT
    assert decision.should_block is True
    assert decision.should_report is False
    assert decision.should_hide is False
    assert decision.report_reason == ""

    safe = css._decision_for(css.CONTENT_PROFILE_PICTURE, {"Hate": 0})
    assert safe.action == css.NO_ACTION
    assert safe.should_block is False


def test_real_image_analyzer_sends_the_documented_request(monkeypatch):
    """The official SDK contract: Analyze Image options carry the bytes, all
    four categories, FourSeverityLevels output, api-version 2024-09-01."""
    monkeypatch.setattr(css, "analyze_image", _REAL_ANALYZE_IMAGE)
    monkeypatch.setattr(css, "moderate_content", _REAL_MODERATE)
    monkeypatch.setattr(css, "moderate_profile_picture", _REAL_MODERATE_PROFILE_PICTURE)
    captured = {}

    class _FakeCategory:
        def __init__(self, category, severity):
            self.category = category
            self.severity = severity

    class _FakeResult:
        categories_analysis = [
            _FakeCategory("Hate", 0),
            _FakeCategory("SelfHarm", 0),
            _FakeCategory("Sexual", 0),
            _FakeCategory("Violence", 6),
        ]

    class _FakeSDKClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

        def analyze_image(self, options, **kwargs):
            captured["options"] = options
            captured["timeout"] = kwargs.get("timeout")
            return _FakeResult()

    monkeypatch.setattr(css, "get_content_safety_client", lambda config: _FakeSDKClient())

    decision = css.moderate_profile_picture(ONE_PIXEL_PNG)

    assert decision.should_block is True
    assert decision.max_severity == 6
    options = captured["options"]
    assert options.image.content == ONE_PIXEL_PNG
    assert list(options.categories) == ["Hate", "SelfHarm", "Sexual", "Violence"]
    assert options.output_type == "FourSeverityLevels"
    assert captured["timeout"] == 5.0
    config = css.config_from_settings(settings)
    assert css.analyze_image_url_for(config).endswith(
        "/contentsafety/image:analyze?api-version=2024-09-01"
    )


def test_an_empty_image_is_refused_before_the_provider(monkeypatch):
    monkeypatch.setattr(css, "analyze_image", _REAL_ANALYZE_IMAGE)

    with pytest.raises(css.ContentSafetyError) as excinfo:
        css.analyze_image(b"")

    assert excinfo.value.code == "invalid_input"


def test_a_malformed_image_response_fails_closed(monkeypatch):
    monkeypatch.setattr(css, "analyze_image", _REAL_ANALYZE_IMAGE)
    monkeypatch.setattr(css, "moderate_content", _REAL_MODERATE)
    monkeypatch.setattr(css, "moderate_profile_picture", _REAL_MODERATE_PROFILE_PICTURE)

    class _FakeResult:
        categories_analysis = [SimpleNamespace(category="Hate", severity=0)]

    class _FakeSDKClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

        def analyze_image(self, options, **kwargs):
            return _FakeResult()

    monkeypatch.setattr(css, "get_content_safety_client", lambda config: _FakeSDKClient())

    with pytest.raises(css.ContentSafetyError) as excinfo:
        css.moderate_profile_picture(ONE_PIXEL_PNG)

    assert excinfo.value.code == "malformed"


# ------------------------------------------------------- the upload path, live


@pytest.mark.asyncio
async def test_a_safe_image_is_stored_and_spends_the_change(db, live_storage):
    make_user(db)

    result = await change_picture(db)

    assert result["message"] == "Profile picture uploaded successfully"
    assert len(live_storage.uploads) == 1
    assert stored_picture(db, "alice").endswith("/profile")
    assert last_change_at(db, "alice") is not None
    assert notifications(db) == []
    assert report_count(db) == 0


@pytest.mark.asyncio
async def test_moderation_runs_before_anything_is_stored(db, live_storage, monkeypatch):
    """The image is analyzed while it is still only bytes in memory."""
    make_user(db)
    _mock_picture_scores(
        monkeypatch,
        {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0},
        order=live_storage.order,
    )

    await change_picture(db)

    assert live_storage.order == ["moderate", "store"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scores",
    [
        {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 2},
        {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0},
        {"Hate": 0, "SelfHarm": 0, "Sexual": 6, "Violence": 0},
    ],
)
async def test_a_violating_image_is_refused_before_storage(db, live_storage, monkeypatch, scores):
    make_user(db)
    _mock_picture_scores(monkeypatch, scores)

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    assert excinfo.value.status_code == 422, excinfo.value.detail
    assert re.search(r"violat.*polic", excinfo.value.detail, re.I), excinfo.value.detail
    assert live_storage.uploads == [], "a violating image must never reach storage"
    assert stored_picture(db, "alice") is None
    assert report_count(db) == 0, "a rejected picture has no report row"

    rows = notifications(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.audience == "all"
    assert row.category == "report"
    assert row.heading == "Profile picture rejected by Azure Content Safety"
    assert "alice" in row.body
    assert "Azure Content Safety" in row.body


@pytest.mark.asyncio
async def test_severity_one_is_not_a_violation(db, live_storage, monkeypatch):
    """The rule is >1, so a 1 -- however it arrives -- proceeds."""
    make_user(db)
    _mock_picture_scores(monkeypatch, {"Hate": 1, "SelfHarm": 0, "Sexual": 0, "Violence": 0})

    result = await change_picture(db)

    assert result["message"] == "Profile picture uploaded successfully"
    assert stored_picture(db, "alice") is not None


@pytest.mark.asyncio
async def test_a_refused_image_spends_exactly_one_attempt(db, live_storage, monkeypatch):
    """The violation consumes the window; a retry is refused by the cooldown,
    before moderation, so neither a second notification nor a second analysis
    can happen."""
    make_user(db)
    calls: list[int] = []

    def _fake(image, *, config=None):
        calls.append(len(image))
        return css._decision_for(css.CONTENT_PROFILE_PICTURE, {"Violence": 6})

    monkeypatch.setattr(css, "moderate_profile_picture", _fake)

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)
    assert excinfo.value.status_code == 422
    consumed_at = last_change_at(db, "alice")
    assert consumed_at is not None

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    assert excinfo.value.status_code == 429
    assert last_change_at(db, "alice") == consumed_at
    assert len(calls) == 1, "the retry was refused before moderation ran"
    assert len(notifications(db)) == 1, "the staff is told once, not once per retry"
    assert live_storage.uploads == []


@pytest.mark.asyncio
async def test_a_violating_image_leaves_the_existing_picture_alone(db, live_storage, monkeypatch):
    make_user(db)
    seed_existing_picture(db, "alice")
    _mock_picture_scores(monkeypatch, {"Sexual": 4})

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    assert excinfo.value.status_code == 422
    assert stored_picture(db, "alice") == "profile_pics/alice/OldIdUnique/profile"
    assert live_storage.destroyed == [], "the picture the row still names must survive"
    assert live_storage.uploads == []


@pytest.mark.asyncio
async def test_a_provider_failure_is_retryable_and_costs_nothing(db, live_storage, monkeypatch):
    """Unknown is not safe and not a violation: 503, nothing stored, nothing
    consumed, no notification -- and the very next attempt can succeed."""
    make_user(db)
    _mock_picture_failure(monkeypatch, code="timeout")

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    assert excinfo.value.status_code == 503, excinfo.value.detail
    assert "unavailable" in excinfo.value.detail.lower()
    assert live_storage.uploads == []
    assert last_change_at(db, "alice") is None, "a provider outage must not spend the change"
    assert notifications(db) == []

    _mock_picture_safe(monkeypatch)
    result = await change_picture(db)
    assert result["message"] == "Profile picture uploaded successfully"


@pytest.mark.asyncio
async def test_missing_configuration_is_a_distinct_503(db, live_storage, monkeypatch):
    make_user(db)
    _mock_picture_failure(monkeypatch, code="missing_config")

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == "Content moderation is not configured."
    assert last_change_at(db, "alice") is None


# ------------------------------------------------------------ over HTTP, as the UI sees it


@pytest.fixture
def picture_owner_client(db):
    """A client whose participant resolves to a row that actually exists."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.auth_service import get_current_user
    from conftest import override_get_current_user

    owner_id = "55555555-5555-5555-5555-555555555555"
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, 'pictuser', 'Picture User', 'pict@user.com', 2, 'Class', 'other') "
                "ON CONFLICT (regno) DO NOTHING"
            ),
            {"id": owner_id},
        )
    owner = UserDetails(
        id=owner_id, regno="pictuser", name="Picture User", email="pict@user.com",
        year=2, department="Class", gender="other",
        account_type=AccountType.USERS.value,
    )
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


def test_over_http_a_violation_is_a_422_the_dialog_can_read(db, live_storage, monkeypatch, picture_owner_client):
    """The frontend shows its violation dialog on 422 + /violat.*polic/i; pin both."""
    _mock_picture_scores(monkeypatch, {"Violence": 2})

    response = picture_owner_client.post(
        "/profile/upload-picture",
        files={"file": ("me.png", ONE_PIXEL_PNG, "image/png")},
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert re.search(r"violat.*polic", detail, re.I), detail
    assert live_storage.uploads == []
    assert last_change_at(db, "pictuser") is not None
    assert len(notifications(db)) == 1
    assert report_count(db) == 0
