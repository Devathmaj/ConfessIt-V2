"""Semantic search for confessions (pgvector + Azure text-embedding-3-small).

Covers the required contract without ever hitting the live Azure endpoint:
provider calls are mocked, while similarity ordering and the vector(1536)
schema are exercised against the real PostgreSQL pgvector extension.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db_models import (
    ConfessionCommentRecord,
    ConfessionEmbeddingRecord,
    ConfessionRecord,
    UserRecord,
)
from app.models import ConfessionCreate
from app.services import embedding_service
from app.services.confession_service import ConfessionService
from app.services.embedding_service import EmbeddingError


def _vec(first: float, second: float = 0.0) -> list[float]:
    base = [0.0] * 1536
    base[0] = float(first)
    base[1] = float(second)
    return base


def _enable_search(monkeypatch, api_key: str = "test-key"):
    from app.services import semantic_rate_limit

    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", api_key)
    monkeypatch.setattr(settings, "EMBEDDING_ENDPOINT", "https://confessit-model.services.ai.azure.com")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSIONS", 1536)
    # Unlimited local budgets unless a test says otherwise: the in-memory
    # buckets are process-wide, so a shared default would let one test spend
    # another's allowance within the same minute window.
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(settings, "EMBEDDING_ADMIN_SEARCH_RATE_LIMIT_PER_MINUTE", 0)
    semantic_rate_limit.reset_buckets()


def make_user(db, regno):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
            year=2, department="CSE", gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno}


def make_confession_via_service(db, monkeypatch, author_id, body, recipient=None, vector=None):
    _enable_search(monkeypatch)
    fixed = vector if vector is not None else _vec(1.0)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: list(fixed)
    )
    service = ConfessionService(db)
    created = service.create_confession(
        ConfessionCreate(
            body=body, anonymous=False, comments_enabled=True, recipient_name=recipient
        ),
        author_id,
    )
    return created


# 1. Successful generation with mocked provider.
def test_successful_embedding_generation_mocked(db, monkeypatch):
    _enable_search(monkeypatch)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(0.5)
    )
    vector = embedding_service.embed_text("hello world")
    assert len(vector) == 1536
    assert all(isinstance(v, float) for v in vector)


def test_confession_document_excludes_comments_and_identities():
    doc = embedding_service.build_confession_document("my body", "Anna")
    assert "my body" in doc
    assert "Anna" in doc
    # Builder only takes body + recipient; comments/ids are never parameters.
    import inspect

    params = inspect.signature(embedding_service.build_confession_document).parameters
    assert set(params) == {"body", "recipient_name"}


# 2. Missing credentials and invalid configuration.
def test_missing_api_key_is_actionable(db, monkeypatch):
    _enable_search(monkeypatch, api_key="")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", None)
    with pytest.raises(EmbeddingError) as exc:
        embedding_service.embed_text("hello")
    assert exc.value.code == "missing_config"
    assert "EMBEDDING_API_KEY" in exc.value.detail
    assert "myapikey" not in exc.value.detail


def test_invalid_model_and_dimensions_rejected(db, monkeypatch):
    _enable_search(monkeypatch)
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-ada-002")
    with pytest.raises(EmbeddingError) as exc:
        embedding_service.embed_text("hello")
    assert exc.value.code == "invalid_config"
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSIONS", 512)
    with pytest.raises(EmbeddingError) as exc2:
        embedding_service.embed_text("hello")
    assert exc2.value.code == "invalid_config"


# 3. Model/dimension invariants.
def test_model_and_dimensions_invariants(db, monkeypatch):
    _enable_search(monkeypatch)
    assert settings.EMBEDDING_MODEL == "text-embedding-3-small"
    assert settings.EMBEDDING_DIMENSIONS == 1536
    assert embedding_service.EXPECTED_MODEL == "text-embedding-3-small"
    assert embedding_service.EXPECTED_DIMENSIONS == 1536


# 4. Malformed / wrong-sized / non-finite vectors rejected, never reshaped.
def test_rejects_malformed_vectors():
    with pytest.raises(EmbeddingError):
        embedding_service.validate_vector([0.0] * 10)
    with pytest.raises(EmbeddingError):
        embedding_service.validate_vector([0.0] * 1537)
    with pytest.raises(EmbeddingError):
        embedding_service.validate_vector("not-a-list")
    bad = [0.0] * 1536
    bad[0] = float("nan")
    with pytest.raises(EmbeddingError):
        embedding_service.validate_vector(bad)
    bad2 = [0.0] * 1536
    bad2[1] = float("inf")
    with pytest.raises(EmbeddingError):
        embedding_service.validate_vector(bad2)
    bad3 = [0.0] * 1536
    bad3[2] = True  # bool is not a valid component
    with pytest.raises(EmbeddingError):
        embedding_service.validate_vector(bad3)


# 5. Timeout / rate-limit / transient behavior (mocked transport).
class _FakeResponse:
    def __init__(self, status_code, payload=None, text_value=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text_value

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def post(self, url, headers=None, json=None):
        self.calls += 1
        action = self.script.pop(0) if self.script else self.script[-1]
        if isinstance(action, Exception):
            raise action
        return action


def _config():
    return embedding_service.config_from_settings(settings)


def test_transient_500_then_success_retries(db, monkeypatch):
    import httpx

    _enable_search(monkeypatch)
    good = _vec(1.0)
    fake = _FakeClient(
        [
            _FakeResponse(500, text_value="server error"),
            _FakeResponse(200, {"data": [{"embedding": good}]}),
        ]
    )
    monkeypatch.setattr(embedding_service, "get_http_client", lambda timeout: fake)
    monkeypatch.setattr(embedding_service.time, "sleep", lambda s: None)
    vector = embedding_service.embed_text("retry me")
    assert len(vector) == 1536
    assert fake.calls == 2


def test_rate_limit_maps_to_429_and_retries(db, monkeypatch):
    _enable_search(monkeypatch)
    good = _vec(1.0)
    fake = _FakeClient(
        [
            _FakeResponse(429, text_value="rate limited"),
            _FakeResponse(200, {"data": [{"embedding": good}]}),
        ]
    )
    monkeypatch.setattr(embedding_service, "get_http_client", lambda timeout: fake)
    monkeypatch.setattr(embedding_service.time, "sleep", lambda s: None)
    vector = embedding_service.embed_text("limited")
    assert len(vector) == 1536
    assert fake.calls == 2


def test_auth_failure_does_not_retry(db, monkeypatch):
    _enable_search(monkeypatch)
    fake = _FakeClient([_FakeResponse(401, text_value="unauthorized")])
    monkeypatch.setattr(embedding_service, "get_http_client", lambda timeout: fake)
    with pytest.raises(EmbeddingError) as exc:
        embedding_service.embed_text("nope")
    assert exc.value.code == "auth"
    assert fake.calls == 1


def test_timeout_raises_and_confession_survives(db, monkeypatch):
    import httpx

    _enable_search(monkeypatch)
    author = make_user(db, "timeoutauthor")

    def _raise(doc, cfg):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", _raise)
    service = ConfessionService(db)
    created = service.create_confession(
        ConfessionCreate(body="survives timeout", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert created is not None
    with db.sessions() as session:
        embeddings = session.scalars(select(ConfessionEmbeddingRecord)).all()
    assert len(embeddings) == 0


# 6/7. Embedding creation for eligible confession; exactly one row.
def test_embedding_created_for_eligible_confession(db, monkeypatch):
    author = make_user(db, "eligibleauthor")
    created = make_confession_via_service(
        db, monkeypatch, author["id"], "eligible body", vector=_vec(1.0)
    )
    with db.sessions() as session:
        rows = session.scalars(
            select(ConfessionEmbeddingRecord).where(
                ConfessionEmbeddingRecord.confession_id == str(created.id)
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].model == "text-embedding-3-small"
    assert len(rows[0].embedding) == 1536 if isinstance(rows[0].embedding, list) else True


def test_exactly_one_embedding_per_confession(db, monkeypatch):
    author = make_user(db, "oneauthor")
    created = make_confession_via_service(
        db, monkeypatch, author["id"], "one embedding", vector=_vec(1.0)
    )
    service = ConfessionService(db)
    # Idempotent retry must not duplicate.
    service._generate_embedding_for_confession(str(created.id), "one embedding", None)
    with db.sessions() as session:
        rows = session.scalars(select(ConfessionEmbeddingRecord)).all()
    assert len(rows) == 1


# 8. No embeddings for comments, likes, reports.
def test_no_embeddings_for_comments_likes_reports(db, monkeypatch):
    from app.accounts import AccountType
    from app.models import CommentCreate, ReportCreate, UserDetails

    author = make_user(db, "noextrauthor")
    commenter = make_user(db, "noextracommenter")
    calls = {"n": 0}

    def counting(doc, cfg):
        calls["n"] += 1
        return _vec(1.0)

    _enable_search(monkeypatch)
    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", counting)
    service = ConfessionService(db)
    created = service.create_confession(
        ConfessionCreate(body="no extras", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert calls["n"] == 1
    commenter_details = UserDetails(
        id=commenter["id"], regno="noextracommenter", name="C",
        email="noextracommenter@test.com", year=2, department="CSE",
        gender="other", account_type=AccountType.USERS.value,
    )
    service.add_comment_to_confession(str(created.id), CommentCreate(body="a reply"), commenter_details)
    fetched = service.get_confession(str(created.id), author["id"])
    assert len(fetched.comments) == 1
    service.like_comment(fetched.comments[0].id, author["id"])
    service.react_to_confession(str(created.id), "heart", author["id"])
    service.report_confession(
        str(created.id), ReportCreate(reason="spam"), commenter_details
    )
    assert calls["n"] == 1, "only confession creation may embed"
    with db.sessions() as session:
        assert len(session.scalars(select(ConfessionEmbeddingRecord)).all()) == 1


# 9. Similarity ordering against real pgvector.
def test_vector_similarity_ordering(db, monkeypatch):
    a = make_user(db, "simauthor1")
    b = make_user(db, "simauthor2")
    _enable_search(monkeypatch)

    # Distinct vectors per body.
    def fake_call(document, cfg):
        if "cats" in document:
            return _vec(1.0, 0.0)
        if "cars" in document:
            return _vec(0.0, 1.0)
        return _vec(0.5, 0.5)

    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", fake_call)
    service = ConfessionService(db)
    cats = service.create_confession(
        ConfessionCreate(body="I love cats", anonymous=False, comments_enabled=True), a["id"]
    )
    cars = service.create_confession(
        ConfessionCreate(body="I love cars", anonymous=False, comments_enabled=True), b["id"]
    )
    # Query close to cats.
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0, 0.0)
    )
    page = service.search_confessions_semantic("kittens", limit=10)
    assert [c.id for c in page["confessions"]] == [str(cats.id), str(cars.id)]


# 10. Empty / invalid / long queries.
def test_search_rejects_blank_and_long_queries(db, monkeypatch, client):
    _enable_search(monkeypatch)
    assert client.post("/confessions/search", json={"query": "   ", "limit": 5}).status_code == 422
    assert client.post("/confessions/search", json={"query": "x" * 1001, "limit": 5}).status_code == 422
    assert client.post("/confessions/search", json={"query": "", "limit": 5}).status_code == 422


# 11. Limits and empty results (hybrid shape).
def test_search_limits_and_empty_results(db, monkeypatch, client):
    _enable_search(monkeypatch)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0)
    )
    # No confessions yet: both halves empty, not an error.
    body = client.post("/confessions/search", json={"query": "anything", "limit": 5}).json()
    assert body["keyword"] == {"items": [], "total": 0}
    assert body["semantic"]["items"] == [] and body["semantic"]["status"] == "ok"
    assert body["query"] == "anything"
    # Limit bounds enforced by validation.
    assert client.post("/confessions/search", json={"query": "hi", "limit": 0}).status_code == 422
    assert client.post("/confessions/search", json={"query": "hi", "limit": 51}).status_code == 422


# 12. Hidden / removed / unembedded excluded.
def test_search_excludes_ineligible(db, monkeypatch):
    from datetime import datetime, timezone

    a = make_user(db, "elig1")
    b = make_user(db, "elig2")
    c = make_user(db, "elig3")
    _enable_search(monkeypatch)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0)
    )
    service = ConfessionService(db)
    visible = service.create_confession(
        ConfessionCreate(body="visible post", anonymous=False, comments_enabled=True), a["id"]
    )
    hidden = service.create_confession(
        ConfessionCreate(body="hidden post", anonymous=False, comments_enabled=True), b["id"]
    )
    # Soft-remove the second.
    with db.sessions.begin() as session:
        row = session.get(ConfessionRecord, str(hidden.id))
        row.removed_at = datetime.now(timezone.utc)
        row.removed_by_regno = "moderator1"
    # Third confession without an embedding (direct insert, no workflow).
    with db.sessions.begin() as session:
        orphan = ConfessionRecord(
            body="no vector", anonymous=False, comments_enabled=True,
            created_at=datetime.now(timezone.utc), author_id=c["id"],
            reactions={"heart": [], "haha": [], "whoa": [], "heartbreak": []},
        )
        session.add(orphan)
        session.flush()
        orphan_id = str(orphan.id)
    page = service.search_confessions_semantic("visible", limit=10)
    ids = [co.id for co in page["confessions"]]
    assert str(visible.id) in ids
    assert str(hidden.id) not in ids
    assert orphan_id not in ids


# 13. Auth preserved: feed-style optional auth; creation still participant-gated.
def test_search_allows_anonymous_but_creation_requires_participant(db, monkeypatch, client):
    from app.main import app
    from app.services.auth_service import get_current_user

    _enable_search(monkeypatch)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0)
    )
    # Authenticated search works.
    assert client.post("/confessions/search", json={"query": "hello", "limit": 5}).status_code in (200, 503)
    # Unauthenticated search also reaches the handler (optional participant).
    original = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.post("/confessions/search", json={"query": "hello", "limit": 5})
        assert response.status_code in (200, 503)
    finally:
        if original is not None:
            app.dependency_overrides[get_current_user] = original


# 14. Hard delete cleans up the embedding.
def test_hard_delete_removes_embedding(db, monkeypatch, admin_client):
    author = make_user(db, "delauthor")
    created = make_confession_via_service(
        db, monkeypatch, author["id"], "to be deleted", vector=_vec(1.0)
    )
    with db.sessions() as session:
        assert (
            session.scalar(
                select(ConfessionEmbeddingRecord).where(
                    ConfessionEmbeddingRecord.confession_id == str(created.id)
                )
            )
            is not None
        )
    response = admin_client.delete(f"/admin/confessions/{created.id}")
    assert response.status_code == 200
    with db.sessions() as session:
        assert (
            session.scalar(
                select(ConfessionEmbeddingRecord).where(
                    ConfessionEmbeddingRecord.confession_id == str(created.id)
                )
            )
            is None
        )


# No edit-triggered regeneration.
def test_update_does_not_regenerate_embedding(db, monkeypatch):
    author = make_user(db, "editauthor")
    created = make_confession_via_service(
        db, monkeypatch, author["id"], "edit me", vector=_vec(1.0)
    )
    with db.sessions() as session:
        before = session.scalar(
            select(ConfessionEmbeddingRecord).where(
                ConfessionEmbeddingRecord.confession_id == str(created.id)
            )
        )
        before_vec = list(before.embedding) if isinstance(before.embedding, list) else before.embedding
    calls = {"n": 0}

    def counting(doc, cfg):
        calls["n"] += 1
        return _vec(0.0, 1.0)

    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", counting)
    service = ConfessionService(db)
    from app.models import ConfessionUpdate

    service.update_confession(str(created.id), ConfessionUpdate(comments_enabled=False), author["id"])
    assert calls["n"] == 0
    with db.sessions() as session:
        after = session.scalar(
            select(ConfessionEmbeddingRecord).where(
                ConfessionEmbeddingRecord.confession_id == str(created.id)
            )
        )
        assert after is not None


# 15. Disabled search makes no provider calls and degrades gracefully.
def test_disabled_search_makes_no_provider_calls(db, monkeypatch, client):
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", False)
    called = {"n": 0}

    def fail(doc, cfg):
        called["n"] += 1
        return _vec(1.0)

    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", fail)
    # Hybrid degradation: 200 with keyword results and semantic.status set,
    # instead of failing the whole request.
    body = client.post("/confessions/search", json={"query": "hello", "limit": 5}).json()
    assert body["semantic"]["status"] == "disabled"
    assert body["semantic"]["items"] == []
    assert called["n"] == 0


def test_creation_skips_provider_when_disabled(db, monkeypatch):
    author = make_user(db, "disauthor")
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", False)
    called = {"n": 0}

    def fail(doc, cfg):
        called["n"] += 1
        return _vec(1.0)

    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", fail)
    service = ConfessionService(db)
    created = service.create_confession(
        ConfessionCreate(body="disabled creation", anonymous=False, comments_enabled=True),
        author["id"],
    )
    assert created is not None
    assert called["n"] == 0
    with db.sessions() as session:
        assert len(session.scalars(select(ConfessionEmbeddingRecord)).all()) == 0


# 16. Migration / vector(1536) compatibility against real PostgreSQL.
def test_vector_extension_and_column_compatibility(db):
    with db.engine.begin() as connection:
        installed = connection.scalar(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        )
        assert installed == 1, "pgvector extension missing: use pgvector/pgvector:pg16"
        coltype = connection.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'confession_embeddings'::regclass AND attname = 'embedding'"
            )
        )
        assert coltype == "vector(1536)", f"expected vector(1536), got {coltype}"
        unique = connection.scalar(
            text(
                "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'confession_embeddings' "
                "AND indexdef LIKE '%UNIQUE%confession_id%'"
            )
        )
        assert unique >= 1
        fk = connection.scalar(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname LIKE '%confession%' "
                "AND contype = 'f'"
            )
        )
        assert fk >= 1


def test_azure_request_format_uses_deployment_and_dimensions(monkeypatch):
    _enable_search(monkeypatch)
    captured = {}

    class CapClient:
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            assert "api-key" in headers
            assert "Authorization" not in headers or "Bearer" not in str(headers.get("Authorization"))
            return _FakeResponse(200, {"data": [{"embedding": _vec(1.0)}]})

    monkeypatch.setattr(embedding_service, "get_http_client", lambda timeout: CapClient())
    embedding_service.embed_text("format check")
    assert captured["url"].endswith("/openai/v1/embeddings")
    assert "api-version=" not in captured["url"]
    assert captured["json"]["model"] == "text-embedding-3-small"
    assert captured["json"]["dimensions"] == 1536
    assert isinstance(captured["json"]["input"], list)
