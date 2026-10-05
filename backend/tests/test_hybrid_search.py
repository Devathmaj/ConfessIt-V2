"""Hybrid keyword + semantic search for users and admins.

One search box, both signals: keyword matches first, similar-in-meaning below.
The provider is always mocked; vector ordering, thresholding, and the
vector(1536) query run against the real PostgreSQL pgvector extension.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.db_models import ConfessionEmbeddingRecord, ConfessionRecord, UserRecord
from app.models import ConfessionCreate
from app.services import embedding_service
from app.services import semantic_rate_limit
from app.services.confession_service import ConfessionService
from app.services.embedding_service import EmbeddingError


def _vec(first: float, second: float = 0.0) -> list[float]:
    base = [0.0] * 1536
    base[0] = float(first)
    base[1] = float(second)
    return base


def _enable(monkeypatch, api_key: str = "test-key"):
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", api_key)
    monkeypatch.setattr(settings, "EMBEDDING_ENDPOINT", "https://confessit-model.services.ai.azure.com")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSIONS", 1536)
    monkeypatch.setattr(settings, "EMBEDDING_MIN_SIMILARITY", 0.30)
    monkeypatch.setattr(settings, "EMBEDDING_SEMANTIC_TOP_K", 10)
    monkeypatch.setattr(settings, "EMBEDDING_MIN_QUERY_CHARS", 3)
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(settings, "EMBEDDING_ADMIN_SEARCH_RATE_LIMIT_PER_MINUTE", 0)
    semantic_rate_limit.reset_buckets()


def make_user(db, regno, name=None):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=name or f"User {regno}", email=f"{regno}@test.com",
            year=2, department="CSE", gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno, "name": row.name}


def create(db, monkeypatch, author_id, body, recipient=None, vector=None):
    _enable(monkeypatch)
    fixed = vector if vector is not None else _vec(1.0)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: list(fixed)
    )
    return ConfessionService(db).create_confession(
        ConfessionCreate(
            body=body, anonymous=False, comments_enabled=True, recipient_name=recipient
        ),
        author_id,
    )


def keyword_ids(page):
    return [str(item.id) for item in page["keyword"]["items"]]


def semantic_ids(page):
    return [str(item.id) for item in page["semantic"]["items"]]


# 1. Keyword hit appears in keyword and NOT in semantic (dedupe).
def test_keyword_hit_deduped_from_semantic(db, monkeypatch):
    author = make_user(db, "dedupauthor")
    created = create(db, monkeypatch, author["id"], "midnight snacks run", vector=_vec(1.0))
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0)
    )
    page = ConfessionService(db).search_confessions(
        "midnight", limit=10, rate_key="dedup-test"
    )
    assert keyword_ids(page) == [str(created.id)]
    assert page["keyword"]["total"] == 1
    assert str(created.id) not in semantic_ids(page)


# 2. Semantic-only match appears under semantic.
def test_semantic_only_match(db, monkeypatch):
    a = make_user(db, "semauthor1")
    b = make_user(db, "semauthor2")
    _enable(monkeypatch)

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
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0, 0.0)
    )
    page = service.search_confessions("kittens", limit=10, rate_key="semonly-test")
    assert page["keyword"]["items"] == [] and page["keyword"]["total"] == 0
    assert semantic_ids(page) == [str(cats.id)]
    assert page["semantic"]["status"] == "ok"
    assert str(cars.id) not in semantic_ids(page)  # similarity 0 < threshold


# 3. Removed confessions: excluded for users, included-and-flagged for admins.
def test_removed_visibility_users_vs_admins(db, monkeypatch, admin_client):
    from datetime import datetime, timezone

    a = make_user(db, "remvis1")
    b = make_user(db, "remvis2")
    _enable(monkeypatch)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0)
    )
    c = make_user(db, "remvis3")
    service = ConfessionService(db)
    visible = service.create_confession(
        ConfessionCreate(body="sunshine plaza days", anonymous=False, comments_enabled=True),
        a["id"],
    )
    hidden = service.create_confession(
        ConfessionCreate(body="sunshine harbor nights", anonymous=False, comments_enabled=True),
        b["id"],
    )
    # Removed and lexically unrelated: reachable only through meaning, and only
    # for staff.
    quiet_removed = service.create_confession(
        ConfessionCreate(body="foghorn lullaby", anonymous=False, comments_enabled=True),
        c["id"],
    )
    with db.sessions.begin() as session:
        for target in (hidden.id, quiet_removed.id):
            row = session.get(ConfessionRecord, str(target))
            row.removed_at = datetime.now(timezone.utc)
            row.removed_by_regno = "moderator1"

    user_page = service.search_confessions("sunshine", limit=10, rate_key="remvis-user")
    assert keyword_ids(user_page) == [str(visible.id)]
    assert str(hidden.id) not in keyword_ids(user_page)
    assert str(hidden.id) not in semantic_ids(user_page)
    assert str(quiet_removed.id) not in semantic_ids(user_page)

    body = admin_client.get(
        "/admin/confessions/search", params={"q": "sunshine", "limit": 10}
    )
    assert body.status_code == 200, body.text
    data = body.json()
    admin_keyword_ids = {item["id"] for item in data["keyword"]["items"]}
    assert {str(visible.id), str(hidden.id)} <= admin_keyword_ids
    hidden_row = next(item for item in data["keyword"]["items"] if item["id"] == str(hidden.id))
    assert hidden_row["content_removed"] is True
    admin_semantic_ids = [item["id"] for item in data["semantic"]["items"]]
    assert str(quiet_removed.id) in admin_semantic_ids
    quiet_sem = next(
        item for item in data["semantic"]["items"] if item["id"] == str(quiet_removed.id)
    )
    assert quiet_sem["content_removed"] is True
    # Dedupe holds on the admin path too.
    assert not (admin_keyword_ids & set(admin_semantic_ids))


# 4. Users cannot find by author identity; admins can (non-anonymous only).
def test_author_identity_visibility(db, monkeypatch, admin_client):
    author = make_user(db, "zxqregno1", name="Zxq Unique")
    _enable(monkeypatch)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0)
    )
    service = ConfessionService(db)
    public = service.create_confession(
        ConfessionCreate(body="ordinary tuesday errands", anonymous=False, comments_enabled=True),
        author["id"],
    )
    with db.sessions.begin() as session:
        anon = ConfessionRecord(
            body="quiet wednesday errands", anonymous=True, comments_enabled=True,
            created_at=datetime.now(timezone.utc), author_id=author["id"],
            reactions={"heart": [], "haha": [], "whoa": [], "heartbreak": []},
        )
        session.add(anon)
        session.flush()
        anon_id = str(anon.id)
    # Orthogonal query vector: no semantic signal, so only keyword could match.
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(0.0, 1.0)
    )
    user_page = service.search_confessions("zxq", limit=10, rate_key="identity-user")
    assert keyword_ids(user_page) == []
    assert semantic_ids(user_page) == []

    data = admin_client.get(
        "/admin/confessions/search", params={"q": "zxq", "limit": 10}
    ).json()
    admin_keyword_ids = [item["id"] for item in data["keyword"]["items"]]
    assert str(public.id) in admin_keyword_ids
    assert anon_id not in admin_keyword_ids


# 5a. Provider failure / 429 degrades to keyword-only with status.
def test_provider_failure_degrades_with_status(db, monkeypatch):
    author = make_user(db, "degradeauthor")
    created = create(db, monkeypatch, author["id"], "ocean breeze morning", vector=_vec(1.0))

    def _failing(code):
        def _raise(doc, cfg):
            raise EmbeddingError("provider blew up", code=code)

        return _raise

    service = ConfessionService(db)
    for code, status in (
        ("rate_limited", "rate_limited"),
        ("unavailable", "unavailable"),
        ("auth", "unavailable"),
        ("network_blocked", "unavailable"),
    ):
        monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", _failing(code))
        page = service.search_confessions("ocean", limit=10, rate_key=f"degrade-{code}")
        assert keyword_ids(page) == [str(created.id)]
        assert page["semantic"] == {"items": [], "status": status}


# 5b. Local bucket deny skips the provider call; admin bucket is independent.
def test_rate_limit_buckets_are_independent(db, monkeypatch, admin_client):
    author = make_user(db, "bucketauthor")
    created = create(db, monkeypatch, author["id"], "mountain trail dusk", vector=_vec(1.0))
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "EMBEDDING_ADMIN_SEARCH_RATE_LIMIT_PER_MINUTE", 0)
    semantic_rate_limit.reset_buckets()
    calls = {"n": 0}

    def counting(doc, cfg):
        calls["n"] += 1
        return _vec(1.0)

    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", counting)
    service = ConfessionService(db)
    first = service.search_confessions("mountain", limit=10, rate_key="shared-key")
    assert first["semantic"]["status"] == "ok"
    second = service.search_confessions("mountain", limit=10, rate_key="shared-key")
    assert second["semantic"] == {"items": [], "status": "rate_limited"}
    assert keyword_ids(second) == [str(created.id)]
    assert calls["n"] == 1, "denied bucket must not call the provider"

    # Staff traffic rides its own (unlimited here) bucket.
    data = admin_client.get(
        "/admin/confessions/search", params={"q": "mountain", "limit": 10}
    ).json()
    assert data["semantic"]["status"] == "ok"
    assert calls["n"] == 2


# 6. Threshold filters unrelated rows; lowering it brings them back.
def test_similarity_threshold_filters_unrelated(db, monkeypatch):
    author = make_user(db, "threshauthor")
    created = create(db, monkeypatch, author["id"], "tuesday ledger lines", vector=_vec(1.0))
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(0.0, 1.0)
    )
    service = ConfessionService(db)
    page = service.search_confessions("qzxwqvjk", limit=10, rate_key="thresh-high")
    assert page["semantic"] == {"items": [], "status": "ok"}
    assert page["keyword"]["total"] == 0

    monkeypatch.setattr(settings, "EMBEDDING_MIN_SIMILARITY", 0.0)
    page = service.search_confessions("qzxwqvjk", limit=10, rate_key="thresh-low")
    assert semantic_ids(page) == [str(created.id)]


# Short queries skip the provider entirely.
def test_short_query_skips_embedding(db, monkeypatch):
    author = make_user(db, "shqauthor")
    created = create(db, monkeypatch, author["id"], "moon glow", vector=_vec(1.0))
    calls = {"n": 0}

    def counting(doc, cfg):
        calls["n"] += 1
        return _vec(1.0)

    monkeypatch.setattr(embedding_service, "_call_azure_embedding_api", counting)
    page = ConfessionService(db).search_confessions("zz", limit=10, rate_key="short-test")
    assert page["semantic"] == {"items": [], "status": "skipped_short"}
    assert calls["n"] == 0
    assert keyword_ids(page) == []  # "zz" matches nothing lexically either


# 7. Blank / too-long queries are 422 on both endpoints.
def test_blank_and_long_queries_422(db, monkeypatch, client, admin_client):
    _enable(monkeypatch)
    for payload in ({"query": "   ", "limit": 5}, {"query": "", "limit": 5},
                    {"query": "x" * 1001, "limit": 5}):
        assert client.post("/confessions/search", json=payload).status_code == 422
    assert admin_client.get("/admin/confessions/search", params={"q": "   "}).status_code == 422
    assert admin_client.get(
        "/admin/confessions/search", params={"q": "x" * 1001}
    ).status_code == 422


# 8. Admin filter/sort still govern the keyword section.
def test_admin_filter_and_sort_keyword(db, monkeypatch, admin_client):
    from app.db_models import ConfessionCommentRecord

    author = make_user(db, "filtadm")
    other = make_user(db, "filtadm2")
    _enable(monkeypatch)
    monkeypatch.setattr(
        embedding_service, "_call_azure_embedding_api", lambda doc, cfg: _vec(1.0)
    )
    service = ConfessionService(db)
    plain = service.create_confession(
        ConfessionCreate(body="plain orchard", anonymous=False, comments_enabled=True),
        author["id"],
    )
    flagged = service.create_confession(
        ConfessionCreate(body="flagged orchard", anonymous=False, comments_enabled=True),
        other["id"],
    )
    with db.sessions.begin() as session:
        session.get(ConfessionRecord, str(flagged.id)).report_count = 2
        session.add(
            ConfessionCommentRecord(
                confession_id=str(flagged.id), user_id=author["id"], body="a reply",
                created_at=datetime.now(timezone.utc),
            )
        )
    data = admin_client.get(
        "/admin/confessions/search",
        params={"q": "orchard", "filter": "reported", "limit": 10},
    ).json()
    assert [item["id"] for item in data["keyword"]["items"]] == [str(flagged.id)]
    assert str(plain.id) not in [item["id"] for item in data["keyword"]["items"]]
    # Semantic never repeats the keyword page.
    keyword_set = {item["id"] for item in data["keyword"]["items"]}
    assert not (keyword_set & {item["id"] for item in data["semantic"]["items"]})

    data = admin_client.get(
        "/admin/confessions/search",
        params={"q": "orchard", "sort": "reports", "limit": 10},
    ).json()
    assert data["keyword"]["items"][0]["id"] == str(flagged.id)
