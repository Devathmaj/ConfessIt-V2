"""Shared embed + vector-query + threshold + dedupe for hybrid search.

One function serves both the public and the staff hybrid paths so the two cannot
drift: the same normalization, short-query skip, per-bucket rate budget, model /
dimension validation, cosine-distance query, similarity floor, top-K cap, and
keyword-dedupe run everywhere. Only visibility differs by caller:

* users pass ``include_removed=False`` and no extra filters;
* admins pass ``include_removed=True`` plus the active status-filter clauses.

Returns ``(candidates, status)`` where each candidate is
``(confession_id, distance, similarity)`` ordered by relevance, and ``status``
is one of ``ok | disabled | unavailable | rate_limited | skipped_short``.
Blank or over-long queries raise :class:`EmbeddingError` (``invalid_input``)
so routers still answer 422; every other failure degrades to a status with an
empty candidate list instead of failing the whole request.
"""

from typing import Any, Callable, Sequence

from sqlalchemy import select

from ..logger import get_logger
from .embedding_service import (
    EmbeddingError,
    SEARCH_QUERY_MAX_LENGTH,
    config_from_settings,
    embed_query,
    normalize_text,
)

logger = get_logger(__name__)


def get_semantic_candidates(
    *,
    sessions: Callable,
    query: str,
    cap: int,
    include_removed: bool,
    extra_filters: Sequence[Any] | None = None,
    exclude_ids: Sequence[str] = (),
    fetch_extra: int = 0,
    bucket: str,
    rate_key: str,
) -> tuple[list[tuple[str, float, float]], str]:
    """Embed ``query`` and return thresholded, deduped vector candidates."""
    from ..config import settings as _settings
    from ..db_models import ConfessionEmbeddingRecord, ConfessionRecord
    from . import semantic_rate_limit as _budgets

    normalized = normalize_text(query or "")
    if not normalized:
        raise EmbeddingError("Search query must not be blank.", code="invalid_input")
    if len(normalized) > SEARCH_QUERY_MAX_LENGTH:
        raise EmbeddingError(
            f"Search query is too long (max {SEARCH_QUERY_MAX_LENGTH} characters).",
            code="invalid_input",
        )
    min_chars = int(_settings.EMBEDDING_MIN_QUERY_CHARS)
    if len(normalized) < min_chars:
        return [], "skipped_short"
    config = config_from_settings(_settings)
    if not config.enabled:
        return [], "disabled"
    allowed, _ = _budgets.check_budget(bucket, rate_key or "anon")
    if not allowed:
        return [], "rate_limited"

    try:
        query_vector = embed_query(normalized, config=config)
    except EmbeddingError as exc:
        if exc.code == "invalid_input":
            raise
        if exc.code == "rate_limited":
            return [], "rate_limited"
        if exc.code == "disabled":
            return [], "disabled"
        logger.warning("Semantic candidates unavailable (%s)", exc.code)
        return [], "unavailable"
    except Exception as exc:
        logger.warning("Semantic candidates failed: %s", type(exc).__name__)
        return [], "unavailable"

    min_similarity = float(_settings.EMBEDDING_MIN_SIMILARITY)
    top_k = max(1, min(int(cap or 1), int(_settings.EMBEDDING_SEMANTIC_TOP_K)))
    # Over-fetch so a caller that dedupes in Python (rather than in SQL via
    # exclude_ids) still fills the promised top-K after dropping overlaps.
    # The return is always truncated back to top_k.
    fetch = min(top_k + max(0, int(fetch_extra or 0)), 100)
    # Cosine distance for normalized embeddings is ``1 - cosine_similarity``.
    max_distance = 1.0 - min_similarity
    excluded = {str(item_id) for item_id in (exclude_ids or ())}

    try:
        with sessions() as session:
            distance = ConfessionEmbeddingRecord.embedding.cosine_distance(query_vector)
            clauses: list[Any] = [distance <= max_distance]
            if not include_removed:
                clauses.append(ConfessionRecord.removed_at.is_(None))
            for clause in extra_filters or ():
                clauses.append(clause)
            if excluded:
                clauses.append(ConfessionRecord.id.notin_(excluded))
            rows = session.execute(
                select(ConfessionRecord.id, distance.label("distance"))
                .join(
                    ConfessionEmbeddingRecord,
                    ConfessionEmbeddingRecord.confession_id == ConfessionRecord.id,
                )
                .where(*clauses)
                .order_by(
                    distance.asc(),
                    ConfessionRecord.created_at.desc(),
                    ConfessionRecord.id.asc(),
                )
                .limit(fetch)
            ).all()
    except Exception as exc:
        logger.warning("Semantic vector query failed: %s", type(exc).__name__)
        return [], "unavailable"

    candidates: list[tuple[str, float, float]] = []
    for row_id, row_distance in rows:
        try:
            dist = float(row_distance)
        except (TypeError, ValueError):
            continue
        candidates.append((str(row_id), dist, 1.0 - dist))
    # Up to ``fetch`` rows: exactly top_k when the caller dedupes in SQL via
    # exclude_ids, or top_k plus spare rows when the caller dedupes in Python
    # against results that only exist after a concurrent query finishes.
    return candidates, "ok"
