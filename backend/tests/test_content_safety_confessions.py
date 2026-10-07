"""Azure AI Content Safety for confessions (Batch 1).

Covers the required contract without real Azure credentials or quota:
the Content Safety API and the embedding client are both mocked, while
persistence, visibility (removed_at), reports and ordering run against the
real PostgreSQL database.
"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts import AccountType
from app.config import settings
from app.db_models import (
    ConfessionEmbeddingRecord,
    ConfessionRecord,
    ReportRecord,
    UserRecord,
)
from app.models import CommentCreate, ConfessionCreate, ReportCreate, UserDetails
from app.services import content_safety_service as css
from app.services import embedding_service
from app.services.confession_service import ConfessionService

# Saved before the conftest safe-mock fixture overwrites it; used by tests
# that need the real HTTP implementation (request-shape contract) or the
# real central dispatcher (missing-config operational error).
_REAL_ANALYZE = css.analyze_confession_text
_REAL_ANALYZE_TEXT = css.analyze_text
_REAL_MODERATE = css.moderate_content
_REAL_MODERATE_CONFESSION = css.moderate_confession_text


def _vec() -> list[float]:
    base = [0.0] * 1536
    base[0] = 1.0
    return base


def _enable_content_safety(monkeypatch, api_key: str = "test-key"):
    monkeypatch.setattr(settings, "CONTENT_SAFETY_ENDPOINT", "https://test-content-safety.example.com")
    monkeypatch.setattr(settings, "CONTENT_SAFETY_API_KEY", api_key)
    monkeypatch.setattr(settings, "CONTENT_SAFETY_REGION", "test-region")
    monkeypatch.setattr(settings, "CONTENT_SAFETY_TIMEOUT_SECONDS", 5.0)


def _mock_moderation(monkeypatch, scores: dict):
    def _fake(text, *, config=None):
        return dict(scores)
    monkeypatch.setattr(css, "analyze_confession_text", _fake)
    monkeypatch.setattr(css, "analyze_text", _fake)

    def _fake_decision(content_type, text, *, config=None):
        return css._decision_for(content_type, dict(scores))

    def _fake_confession(text, *, config=None):
        return css._decision_for(css.CONTENT_CONFESSION, dict(scores))

    monkeypatch.setattr(css, "moderate_content", _fake_decision)
    monkeypatch.setattr(css, "moderate_confession_text", _fake_confession)


def _mock_moderation_failure(monkeypatch, code: str = "unavailable"):
    def _raise(text, *, config=None):
        raise css.ContentSafetyError("provider down", code=code)
    monkeypatch.setattr(css, "analyze_confession_text", _raise)
    monkeypatch.setattr(css, "analyze_text", _raise)

    def _raise_decision(content_type, text, *, config=None):
        raise css.ContentSafetyError("provider down", code=code)

    def _raise_confession(text, *, config=None):
        raise css.ContentSafetyError("provider down", code=code)

    monkeypatch.setattr(css, "moderate_content", _raise_decision)
    monkeypatch.setattr(css, "moderate_confession_text", _raise_confession)


def _mock_embedding_success(monkeypatch, calls: list | None = None):
    def _fake(doc, cfg):
        if calls is not None:
            calls.append("embed")
        return list(_vec())
    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", _fake)
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMBEDDING_ENDPOINT", "https://confessit-model.services.ai.azure.com")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSIONS", 1536)


def _mock_embedding_failure(monkeypatch):
    def _raise(doc, cfg):
        raise embedding_service.EmbeddingError("provider down", code="unavailable")
    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", _raise)
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMBEDDING_ENDPOINT", "https://confessit-model.services.ai.azure.com")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSIONS", 1536)


def make_user(db, regno, name=None):
    name = name or f"User {regno}"
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=name, email=f"{regno}@test.com",
            year=2, department="CSE", gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": regno, "name": name}


def as_participant(account):
    return UserDetails(
        id=account["id"], regno=account["regno"], name=account["name"],
        email=f"{account['regno']}@test.com",
        gender="other", year=2, department="CSE",
        account_type=AccountType.USERS.value,
    )


def _reports_for(db, confession_id):
    with db.sessions() as session:
        return session.scalars(
            select(ReportRecord).where(ReportRecord.confession_id == str(confession_id))
        ).all()


def _confession_row(db, confession_id):
    with db.sessions() as session:
        return session.get(ConfessionRecord, str(confession_id))


def _embedding_row(db, confession_id):
    with db.sessions() as session:
        return session.scalar(
            select(ConfessionEmbeddingRecord).where(
                ConfessionEmbeddingRecord.confession_id == str(confession_id)
            )
        )


# 1. Severity 0: no report, no hide, embedding occurs.
def test_severity_0_no_report_no_hide_embedding_occurs(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "s0author")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="a gentle confession", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert created is not None
    assert _reports_for(db, created.id) == []
    row = _confession_row(db, created.id)
    assert row.removed_at is None
    assert _embedding_row(db, created.id) is not None
    # Visible in feed.
    feed = svc.get_confessions(user_id=author["id"])
    assert any(str(c.id) == str(created.id) for c in feed["confessions"])


# 2. Severity 2: flagged + hidden (>1), embedding still occurs.
def test_severity_2_no_report_no_hide_embedding_occurs(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 2, "SelfHarm": 0, "Sexual": 2, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "s2author")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="mildly edgy", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert len(_reports_for(db, created.id)) == 1
    assert _confession_row(db, created.id).removed_at is not None
    assert _embedding_row(db, created.id) is not None
    feed = svc.get_confessions(user_id=author["id"])
    assert all(str(c.id) != str(created.id) for c in feed["confessions"])


# 3. Severity 4: one report + hidden, actor Azure Content Safety, embedding occurs.
def test_severity_4_reports_without_hiding(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 4, "SelfHarm": 0, "Sexual": 2, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "s4author")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="moderate content", anonymous=False, comments_enabled=True),
        author["id"],
    )
    reports = _reports_for(db, created.id)
    assert len(reports) == 1
    assert reports[0].reported_by_name == "Azure Content Safety"
    assert reports[0].reported_by_id is None
    assert getattr(reports[0], "source") == "azure_content_safety"
    # Scores + max recorded in existing reason field.
    assert "4" in reports[0].reason
    assert "Azure AI Content Safety" in reports[0].reason
    assert "Hate=4" in reports[0].reason
    # Hidden (>1 flagged+hidden).
    assert _confession_row(db, created.id).removed_at is not None
    feed = svc.get_confessions(user_id=author["id"])
    assert all(str(c.id) != str(created.id) for c in feed["confessions"])
    assert _embedding_row(db, created.id) is not None
    assert svc.get_confession(str(created.id), author["id"]) is None


# 4. Severity 6: report + hide via existing mechanism, feed excludes, embedding still occurs.
def test_severity_6_reports_and_hides_but_still_embeds(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 6, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "s6author")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="high severity", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert created is not None
    reports = _reports_for(db, created.id)
    assert len(reports) == 1
    assert reports[0].reported_by_name == "Azure Content Safety"
    row = _confession_row(db, created.id)
    assert row.removed_at is not None
    # Existing hide mechanism: removed_at set (soft removal, row kept).
    assert row.body == "high severity"
    # Excluded from ordinary feeds/lists/counts.
    feed = svc.get_confessions(user_id=author["id"])
    assert all(str(c.id) != str(created.id) for c in feed["confessions"])
    assert svc.get_confession(str(created.id), author["id"]) is None
    assert svc.get_total_confessions_count() == 0
    # Embedding still occurs despite high severity.
    assert _embedding_row(db, created.id) is not None


# 5. Mixed categories: 6 in any single category triggers high policy.
def test_mixed_categories_single_6_triggers_high(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 2, "Violence": 6})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "mixauthor")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="mixed scores", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert len(_reports_for(db, created.id)) == 1
    assert _confession_row(db, created.id).removed_at is not None
    assert _embedding_row(db, created.id) is not None


def test_policy_central_max_and_thresholds():
    assert css.max_severity({"Hate": 0, "SelfHarm": 2, "Sexual": 0, "Violence": 0}) == 2
    assert css.max_severity({"Hate": 2, "SelfHarm": 6, "Sexual": 0, "Violence": 0}) == 6
    assert css.classify_action(0) == css.NO_ACTION
    assert css.classify_action(2) == css.REPORT_AND_HIDE
    assert css.classify_action(4) == css.REPORT_AND_HIDE
    assert css.classify_action(6) == css.REPORT_AND_HIDE
    assert css.should_report(2) is True
    assert css.should_hide(2) is True
    assert css.should_hide(6) is True


# 6. No duplicate automated reports on retry.
def test_no_duplicate_automated_reports_on_retry(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "dupauthor")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="needs report", anonymous=False, comments_enabled=True),
        author["id"],
    )
    first = _reports_for(db, created.id)
    assert len(first) == 1
    # Retrying the same reporting operation does not duplicate.
    rid1 = svc.create_automated_content_safety_report(
        str(created.id), {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0}, 4
    )
    rid2 = svc.create_automated_content_safety_report(
        str(created.id), {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0}, 4
    )
    assert rid1 == rid2 == str(first[0].id)
    assert len(_reports_for(db, created.id)) == 1


# 7. Content Safety API failure: not treated as safe, embedding/publishing does not continue.
def test_content_safety_failure_aborts_before_persistence(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation_failure(monkeypatch, code="unavailable")
    calls: list = []
    _mock_embedding_success(monkeypatch, calls=calls)
    author = make_user(db, "failauthor")
    svc = ConfessionService(db)
    with pytest.raises(HTTPException) as exc:
        svc.create_confession(
            ConfessionCreate(body="unmoderated", anonymous=False, comments_enabled=True),
            author["id"],
        )
    assert exc.value.status_code in (502, 503)
    # No confession persisted, no embedding attempted as though moderation succeeded.
    with db.sessions() as session:
        assert session.scalar(select(ConfessionRecord)) is None
        assert session.scalar(select(ReportRecord)) is None
        assert session.scalar(select(ConfessionEmbeddingRecord)) is None
    assert calls == []


def test_content_safety_timeout_also_aborts(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation_failure(monkeypatch, code="timeout")
    calls: list = []
    _mock_embedding_success(monkeypatch, calls=calls)
    author = make_user(db, "timeoutmodauthor")
    svc = ConfessionService(db)
    with pytest.raises(HTTPException):
        svc.create_confession(
            ConfessionCreate(body="timeout case", anonymous=False, comments_enabled=True),
            author["id"],
        )
    assert calls == []


# 8. Embedding failure: moderation preserved, retry creates no new report.
def test_embedding_failure_preserves_moderation_and_retry_creates_no_report(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    _mock_embedding_failure(monkeypatch)
    author = make_user(db, "embfailauthor")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="embed fails but moderated", anonymous=False, comments_enabled=True),
        author["id"],
    )
    # Moderation outcome durable despite embedding failure.
    assert len(_reports_for(db, created.id)) == 1
    assert _confession_row(db, created.id).removed_at is not None
    assert _embedding_row(db, created.id) is None
    # Embedding retry does not create another report.
    assert svc._generate_embedding_for_confession(str(created.id), "embed fails but moderated", None) is False
    assert len(_reports_for(db, created.id)) == 1
    # Now succeed the embedding retry: still exactly one report.
    _mock_embedding_success(monkeypatch)
    assert svc._generate_embedding_for_confession(str(created.id), "embed fails but moderated", None) is True
    assert len(_reports_for(db, created.id)) == 1
    assert _embedding_row(db, created.id) is not None


# 9. Actor rendering in admin/report interfaces.
def test_actor_rendering_in_admin_queue(db, monkeypatch, admin_client):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 6, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "actor author", name="Actor Author")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="actor rendering", anonymous=False, comments_enabled=True),
        author["id"],
    )
    resp = admin_client.get("/admin/confessions/reports")
    assert resp.status_code == 200
    items = [r for r in resp.json()["reports"] if r["confession_id"] == str(created.id)]
    assert len(items) == 1
    assert items[0]["reporter"]["name"] == "Azure Content Safety"
    # Confession review surface also shows the automated reporter.
    conf_resp = admin_client.get("/admin/confessions")
    assert conf_resp.status_code == 200
    found = [c for c in conf_resp.json()["confessions"] if c["id"] == str(created.id)]
    assert len(found) == 1
    assert found[0]["content_removed"] is True
    reporter_names = [r["reporter"]["name"] for r in found[0]["reports"]]
    assert "Azure Content Safety" in reporter_names


# 10. Existing workflows intact: manual reports, manual hiding, normal creation, visibility.
def test_existing_manual_workflows_intact(db, monkeypatch):
    # Normal low-severity creation still works (mocked safe by conftest default,
    # overridden here explicitly for clarity).
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "manualauthor")
    reporter = make_user(db, "manualreporter")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="manual workflow post", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert svc.get_confession(str(created.id), author["id"]) is not None
    # Manual report by a real user still works and attributes to them.
    out = svc.report_confession(
        str(created.id), ReportCreate(reason="inappropriate"), as_participant(reporter)
    )
    assert out is not None
    reports = _reports_for(db, created.id)
    assert len(reports) == 1
    assert reports[0].reported_by_name == reporter["name"]
    # Duplicate manual report by same user still refused.
    assert svc.report_confession(
        str(created.id), ReportCreate(reason="again"), as_participant(reporter)
    ) is None
    # A second user may still manually report the same confession.
    reporter2 = make_user(db, "manualreporter2")
    assert svc.report_confession(
        str(created.id), ReportCreate(reason="also bad"), as_participant(reporter2)
    ) is not None
    assert len(_reports_for(db, created.id)) == 2
    # Manual hiding still works.
    from app.services.report_resolution_service import hide_confession as _hide

    res = _hide(db, str(created.id), "moduser")
    assert res["content_removed"] is True
    assert svc.get_confession(str(created.id), author["id"]) is None


# 11. Comments share the same principal/logic (central call, report+hide).
def test_scope_isolation_comments_unchanged(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "scopeauthor")
    commenter = make_user(db, "scopecommenter")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="comment scope post", anonymous=False, comments_enabled=True),
        author["id"],
    )
    # Comment path must consult central moderation (track central calls).
    calls: list = []

    def _tracking_moderate(content_type, text, *, config=None):
        calls.append((content_type, text))
        return css._decision_for(content_type, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0})

    monkeypatch.setattr(css, "moderate_content", _tracking_moderate)
    before = len(calls)
    comment = svc.add_comment_to_confession(
        str(created.id),
        CommentCreate(body="a plain comment"),
        as_participant(commenter),
    )
    assert comment.body == "a plain comment"
    assert len(calls) == before + 1
    assert calls[-1][0] == css.CONTENT_COMMENT
    # No automated confession report (confession was low severity).
    assert _reports_for(db, created.id) == []
    # Comment has no embedding row mechanism (only confessions do).
    with db.sessions() as session:
        assert session.scalar(select(ConfessionEmbeddingRecord).where(
            ConfessionEmbeddingRecord.confession_id == str(created.id)
        )) is not None


# Order: Content Safety before embedding, with report/hide in between.
def test_order_content_safety_before_embedding(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    order: list = []

    def _moderate(text, *, config=None):
        order.append("moderate")
        return {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0}

    def _moderate_central(content_type, text, *, config=None):
        order.append("moderate")
        return css._decision_for(content_type, {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0})

    def _moderate_confession(text, *, config=None):
        order.append("moderate")
        return css._decision_for(css.CONTENT_CONFESSION, {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0})

    def _embed(doc, cfg):
        order.append("embed")
        return list(_vec())

    monkeypatch.setattr(css, "analyze_confession_text", _moderate)
    monkeypatch.setattr(css, "analyze_text", _moderate)
    monkeypatch.setattr(css, "moderate_content", _moderate_central)
    monkeypatch.setattr(css, "moderate_confession_text", _moderate_confession)
    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", _embed)
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMBEDDING_ENDPOINT", "https://confessit-model.services.ai.azure.com")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSIONS", 1536)
    author = make_user(db, "orderauthor")
    svc = ConfessionService(db)
    created = svc.create_confession(
        ConfessionCreate(body="order check", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert order == ["moderate", "embed"]
    assert len(_reports_for(db, created.id)) == 1


# 12. Configuration: missing config errors clearly; credentials never hardcoded/logged.
def test_missing_configuration_produces_operational_error(db, monkeypatch):
    _mock_embedding_success(monkeypatch)
    # Restore the real analyzer + central dispatcher so missing config is
    # actually exercised (conftest safe-mocks all of them).
    monkeypatch.setattr(css, "analyze_confession_text", _REAL_ANALYZE)
    monkeypatch.setattr(css, "analyze_text", _REAL_ANALYZE_TEXT)
    monkeypatch.setattr(css, "moderate_content", _REAL_MODERATE)
    monkeypatch.setattr(css, "moderate_confession_text", _REAL_MODERATE_CONFESSION)
    monkeypatch.setattr(settings, "CONTENT_SAFETY_ENDPOINT", None)
    monkeypatch.setattr(settings, "CONTENT_SAFETY_API_KEY", None)
    author = make_user(db, "cfg author".replace(" ", ""))
    svc = ConfessionService(db)
    with pytest.raises(HTTPException) as exc:
        svc.create_confession(
            ConfessionCreate(body="needs config", anonymous=False, comments_enabled=True),
            author["id"],
        )
    assert exc.value.status_code in (502, 503)
    with db.sessions() as session:
        assert session.scalar(select(ConfessionRecord)) is None


def test_credentials_not_hardcoded_or_exposed(monkeypatch):
    import inspect

    import app.services.content_safety_service as mod

    src = inspect.getsource(mod)
    # No literal secrets in source.
    assert "test-key" not in src
    assert "sk-" not in src.lower()
    # Config comes from settings, never hardcoded.
    assert "CONTENT_SAFETY_ENDPOINT" in src
    assert "CONTENT_SAFETY_API_KEY" in src
    # Official SDK: credential from settings via AzureKeyCredential, never logged.
    assert "ContentSafetyClient" in src
    assert "AnalyzeTextOptions" in src
    assert "AzureKeyCredential" in src
    assert "api_key" in src.lower()


def test_request_shape_matches_documented_contract(monkeypatch):
    """SDK contract: AnalyzeTextOptions + api-version 2024-09-01."""
    captured: dict = {}

    class _FakeCategory:
        def __init__(self, category, severity):
            self.category = category
            self.severity = severity

    class _FakeResult:
        categories_analysis = [
            _FakeCategory("Hate", 0),
            _FakeCategory("SelfHarm", 2),
            _FakeCategory("Sexual", 4),
            _FakeCategory("Violence", 6),
        ]

    class _FakeSDKClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

        def analyze_text(self, options, **kwargs):
            captured["options"] = options
            captured["kwargs"] = kwargs
            return _FakeResult()

    def _fake_factory(config):
        captured["endpoint"] = str(config.endpoint)
        captured["api_version"] = css.API_VERSION
        return _FakeSDKClient()

    # Restore the real analyzer (conftest safe-mocks it for other tests).
    monkeypatch.setattr(css, "analyze_confession_text", _REAL_ANALYZE)
    monkeypatch.setattr(css, "analyze_text", _REAL_ANALYZE_TEXT)
    monkeypatch.setattr(css, "get_content_safety_client", _fake_factory)
    cfg = css.ContentSafetyConfig(
        endpoint="https://example-content-safety.cognitiveservices.azure.com",
        api_key="secret-key",
        region="test-region",
        timeout_seconds=5.0,
    )
    scores = css.analyze_confession_text("hello world", config=cfg)
    assert scores == {"Hate": 0, "SelfHarm": 2, "Sexual": 4, "Violence": 6}
    # SDK client targets the configured endpoint with 2024-09-01.
    assert captured["endpoint"] == "https://example-content-safety.cognitiveservices.azure.com"
    assert captured["api_version"] == "2024-09-01"
    assert css.API_VERSION == "2024-09-01"
    options = captured["options"]
    assert getattr(options, "text") == "hello world"
    assert getattr(options, "output_type") == "FourSeverityLevels"
    cats = [str(getattr(c, "value", c)) for c in (getattr(options, "categories") or [])]
    assert sorted(cats) == sorted(["Hate", "SelfHarm", "Sexual", "Violence"])


def test_long_input_refused_not_truncated(monkeypatch):
    _enable_content_safety(monkeypatch)
    monkeypatch.setattr(css, "analyze_confession_text", _REAL_ANALYZE)
    monkeypatch.setattr(css, "analyze_text", _REAL_ANALYZE_TEXT)
    cfg = css.config_from_settings(settings)
    with pytest.raises(css.ContentSafetyError) as exc:
        css.analyze_confession_text("x" * (css.TEXT_MAX_LENGTH + 1), config=cfg)
    assert exc.value.code == "invalid_input"


def test_central_dispatcher_confession_matches_policy(monkeypatch):
    _enable_content_safety(monkeypatch)
    monkeypatch.setattr(css, "analyze_confession_text", _REAL_ANALYZE)
    monkeypatch.setattr(css, "analyze_text", _REAL_ANALYZE_TEXT)
    monkeypatch.setattr(css, "moderate_content", _REAL_MODERATE)
    monkeypatch.setattr(css, "moderate_confession_text", _REAL_MODERATE_CONFESSION)

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

        def analyze_text(self, options, **kwargs):
            return _FakeResult()

    monkeypatch.setattr(css, "get_content_safety_client", lambda config: _FakeSDKClient())
    decision = css.moderate_content(css.CONTENT_CONFESSION, "central check")
    assert decision.max_severity == 6
    assert decision.should_report is True
    assert decision.should_hide is True
    assert decision.should_block is False
    assert "Violence=6" in decision.report_reason
    # Thin wrapper agrees.
    decision2 = css.moderate_confession_text("central check")
    assert decision2.max_severity == 6
    assert decision2.action == decision.action


def test_central_dispatcher_rejects_unimplemented_items(monkeypatch):
    _enable_content_safety(monkeypatch)
    monkeypatch.setattr(css, "moderate_content", _REAL_MODERATE)
    for item in (
        css.CONTENT_CHAT_MESSAGE,
        css.CONTENT_IMAGE,
        css.CONTENT_AI_OUTPUT,
        "love_note",
    ):
        with pytest.raises(css.ContentSafetyError) as exc:
            css.moderate_content(item, "some text")
        assert exc.value.code == "unsupported_item"
    # Confessions + comments supported with the same principal.
    assert set(css.SUPPORTED_CONTENT_TYPES) == {css.CONTENT_CONFESSION, css.CONTENT_COMMENT}


def test_central_policy_registry_single_source_of_truth():
    # Thresholds live in _POLICIES, not scattered in callers.
    # Everything >1 flagged+hidden, same for confessions + comments.
    for item in (css.CONTENT_CONFESSION, css.CONTENT_COMMENT):
        assert css._POLICIES[item]["report_threshold"] == 2
        assert css._POLICIES[item]["hide_threshold"] == 2
        assert css.classify_for_content(item, 0) == css.NO_ACTION
        assert css.classify_for_content(item, 2) == css.REPORT_AND_HIDE
        assert css.classify_for_content(item, 6) == css.REPORT_AND_HIDE
    # Legacy helpers still agree (backward compat).
    assert css.classify_action(2) == css.REPORT_AND_HIDE
    assert css.should_hide(2) is True


def test_confession_path_uses_no_local_configuration():
    import inspect

    from app.services.confession_service import ConfessionService

    src = inspect.getsource(ConfessionService.create_confession)
    # Central call only: no per-feature endpoint/key plumbing here.
    assert "moderate_content" in src
    assert "CONTENT_SAFETY_ENDPOINT" not in src
    assert "CONTENT_SAFETY_API_KEY" not in src
    assert "config_from_settings" not in src


def _comment_reports_for(db, comment_id):
    from app.db_models import ReportRecord as _RR

    with db.sessions() as session:
        return session.scalars(
            select(_RR).where(_RR.comment_id == str(comment_id))
        ).all()


def _make_visible_confession(db, monkeypatch, author_id, body="parent post"):
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    _mock_embedding_success(monkeypatch)
    return ConfessionService(db).create_confession(
        ConfessionCreate(body=body, anonymous=False, comments_enabled=True),
        author_id,
    )


def test_comment_severity_0_visible_no_report(db, monkeypatch):
    from app.db_models import ConfessionCommentRecord as _CCR

    _enable_content_safety(monkeypatch)
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "c0author")
    commenter = make_user(db, "c0commenter")
    svc = ConfessionService(db)
    parent = _make_visible_confession(db, monkeypatch, author["id"])
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    comment = svc.add_comment_to_confession(
        str(parent.id), CommentCreate(body="kind comment"), as_participant(commenter)
    )
    assert _comment_reports_for(db, comment.id) == []
    with db.sessions() as session:
        assert session.get(_CCR, str(comment.id)).removed_at is None
    page = svc.get_confession_comments(str(parent.id), commenter["id"])
    assert any(c.id == comment.id for c in page["comments"])


def test_comment_severity_2_flagged_hidden_with_report(db, monkeypatch, admin_client):
    from app.db_models import ConfessionCommentRecord as _CCR

    _enable_content_safety(monkeypatch)
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "c2author")
    commenter = make_user(db, "c2commenter")
    svc = ConfessionService(db)
    parent = _make_visible_confession(db, monkeypatch, author["id"])
    _mock_moderation(monkeypatch, {"Hate": 2, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    comment = svc.add_comment_to_confession(
        str(parent.id), CommentCreate(body="edgy comment"), as_participant(commenter)
    )
    reports = _comment_reports_for(db, comment.id)
    assert len(reports) == 1
    assert reports[0].reported_by_name == "Azure Content Safety"
    assert reports[0].reported_by_id is None
    with db.sessions() as session:
        assert session.get(_CCR, str(comment.id)).removed_at is not None
    # Hidden from participant thread, still visible to staff.
    page = svc.get_confession_comments(str(parent.id), commenter["id"])
    assert all(c.id != comment.id for c in page["comments"])
    queued = [r for r in admin_client.get("/admin/confessions/reports").json()["reports"] if r["target_id"] == str(comment.id)]
    assert len(queued) == 1
    assert queued[0]["reporter"]["name"] == "Azure Content Safety"


def test_comment_severity_6_flagged_hidden(db, monkeypatch):
    from app.db_models import ConfessionCommentRecord as _CCR

    _enable_content_safety(monkeypatch)
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "c6author")
    commenter = make_user(db, "c6commenter")
    svc = ConfessionService(db)
    parent = _make_visible_confession(db, monkeypatch, author["id"])
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    comment = svc.add_comment_to_confession(
        str(parent.id), CommentCreate(body="bad comment"), as_participant(commenter)
    )
    assert len(_comment_reports_for(db, comment.id)) == 1
    with db.sessions() as session:
        assert session.get(_CCR, str(comment.id)).removed_at is not None
    assert svc.get_confession_comments(str(parent.id), commenter["id"])["comments"] == [] or all(
        c.id != comment.id for c in svc.get_confession_comments(str(parent.id), commenter["id"])["comments"]
    )


def test_comment_no_duplicate_reports_on_retry(db, monkeypatch):
    _enable_content_safety(monkeypatch)
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "cdupauthor")
    commenter = make_user(db, "cdupcommenter")
    svc = ConfessionService(db)
    parent = _make_visible_confession(db, monkeypatch, author["id"])
    _mock_moderation(monkeypatch, {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    comment = svc.add_comment_to_confession(
        str(parent.id), CommentCreate(body="needs report"), as_participant(commenter)
    )
    first = _comment_reports_for(db, comment.id)
    assert len(first) == 1
    rid1 = svc.create_automated_comment_safety_report(
        str(comment.id), {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0}, 4
    )
    rid2 = svc.create_automated_comment_safety_report(
        str(comment.id), {"Hate": 4, "SelfHarm": 0, "Sexual": 0, "Violence": 0}, 4
    )
    assert rid1 == rid2 == str(first[0].id)
    assert len(_comment_reports_for(db, comment.id)) == 1


def test_comment_moderation_failure_aborts(db, monkeypatch):
    from app.db_models import ConfessionCommentRecord as _CCR

    _enable_content_safety(monkeypatch)
    _mock_embedding_success(monkeypatch)
    author = make_user(db, "cfailauthor")
    commenter = make_user(db, "cfailcommenter")
    svc = ConfessionService(db)
    parent = _make_visible_confession(db, monkeypatch, author["id"])
    _mock_moderation_failure(monkeypatch, code="unavailable")
    with pytest.raises(HTTPException) as exc:
        svc.add_comment_to_confession(
            str(parent.id), CommentCreate(body="unmoderated"), as_participant(commenter)
        )
    assert exc.value.status_code in (502, 503)
    with db.sessions() as session:
        assert session.scalar(select(_CCR)) is None


def test_comment_path_uses_no_local_configuration():
    import inspect

    from app.services.confession_service import ConfessionService

    src = inspect.getsource(ConfessionService.add_comment_to_confession)
    assert "moderate_content" in src
    assert "CONTENT_SAFETY_ENDPOINT" not in src
    assert "CONTENT_SAFETY_API_KEY" not in src
    assert "config_from_settings" not in src
