"""T1 backend unit tests — app.memory.retriever + app.memory.embeddings.

Scope (per ownership): pure-logic coverage of the hybrid retriever's RRF merge,
its empty-query recency fallback, its no-hit recency fallback, and the embedding
helper's shape/delegation. NO live DB and NO live model gateway:

  * ``gateway.embed`` is monkeypatched to a deterministic stub.
  * The async SQLAlchemy ``session.execute`` is replaced with a fake whose
    ``.scalars().all()`` returns canned ``Memory``-shaped rows, in call order.

These tests are pure unit tests: they never reach Postgres or Ollama, so they
collect and run on a bare host while the implementation fleet edits source.

Test style: Arrange-Act-Assert with descriptive names.
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from typing import Any

import pytest

from app.db.models import EventType
from app.memory import embeddings as embeddings_mod
from app.memory import retriever as retriever_mod
from app.memory.retriever import _RRF_K, retrieve


# --------------------------------------------------------------------------- #
# Fakes: Memory rows + async session whose execute returns canned scalars.
# --------------------------------------------------------------------------- #


def _make_memory_row(
    *,
    mid: str,
    event_type: EventType = EventType.battle,
    summary: str = "summary",
    content: str = "content",
    salience: float = 0.5,
    created_at: _dt.datetime | None = None,
) -> SimpleNamespace:
    """Build a Memory-shaped duck-typed row.

    ``_memory_to_dict`` only touches: id, event_type (.value), summary, content,
    salience, created_at (.isoformat). A SimpleNamespace with those attributes is
    sufficient and avoids constructing a real SQLModel/pgvector ORM instance.
    """
    if created_at is None:
        created_at = _dt.datetime(2026, 6, 20, 12, 0, 0)
    return SimpleNamespace(
        id=mid,
        event_type=event_type,
        summary=summary,
        content=content,
        salience=salience,
        created_at=created_at,
    )


class _FakeScalarResult:
    """Mimics the object returned by AsyncSession.execute().

    Only ``.scalars().all()`` is exercised by the retriever, so that's all we
    implement. Returns whatever canned rows it was constructed with.
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeScalarResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """Stand-in for AsyncSession.

    ``execute`` is async and pops the next canned result set from a queue, so a
    test can script the exact rows each successive query returns (vector search,
    then keyword search, then any fallback).
    """

    def __init__(self, result_batches: list[list[Any]]) -> None:
        self._batches = list(result_batches)
        self.execute_call_count = 0
        self.executed_statements: list[Any] = []

    async def execute(self, statement: Any) -> _FakeScalarResult:
        self.execute_call_count += 1
        self.executed_statements.append(statement)
        if self._batches:
            return _FakeScalarResult(self._batches.pop(0))
        return _FakeScalarResult([])


@pytest.fixture
def stub_embed(monkeypatch: pytest.MonkeyPatch):
    """Patch retriever's embed() to a deterministic 768-dim vector, no gateway.

    The retriever calls ``embed([query])`` (imported into its namespace), so we
    patch the name on the retriever module.
    """
    calls: list[list[str]] = []

    async def _fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[0.1] * 768 for _ in texts]

    monkeypatch.setattr(retriever_mod, "embed", _fake_embed)
    return calls


# --------------------------------------------------------------------------- #
# embeddings.embed — shape + delegation
# --------------------------------------------------------------------------- #


async def test_embed_delegates_to_gateway_with_nomic_model(
    gateway_mock,
) -> None:
    # Arrange: gateway_mock patches the singleton gateway.embed to a stub that
    # records calls and returns EMBED_DIM-length vectors.
    texts = ["alpha", "beta beta"]

    # Act
    vectors = await embeddings_mod.embed(texts)

    # Assert: delegated to the gateway with the expected model + texts.
    assert gateway_mock.embed_calls == [
        {"texts": texts, "model": "nomic-embed-text"}
    ]
    # Shape: one vector per input.
    assert len(vectors) == len(texts)


async def test_embed_returns_768_dim_vectors(gateway_mock) -> None:
    # Arrange
    texts = ["one", "two", "three"]

    # Act
    vectors = await embeddings_mod.embed(texts)

    # Assert: every vector has the gateway's embedding dimensionality.
    assert all(len(v) == gateway_mock.EMBED_DIM for v in vectors)
    assert gateway_mock.EMBED_DIM == 768


async def test_embed_empty_list_returns_empty(gateway_mock) -> None:
    # Arrange / Act
    vectors = await embeddings_mod.embed([])

    # Assert
    assert vectors == []
    assert gateway_mock.embed_calls == [{"texts": [], "model": "nomic-embed-text"}]


# --------------------------------------------------------------------------- #
# retriever.retrieve — empty-query recency fallback
# --------------------------------------------------------------------------- #


async def test_empty_query_returns_recency_ordered_memories(
    stub_embed,
) -> None:
    # Arrange: empty query => single recency query; embed must NOT be called.
    recent = [
        _make_memory_row(mid="m1", summary="newest"),
        _make_memory_row(mid="m2", summary="older"),
    ]
    session = _FakeSession([recent])

    # Act
    out = await retrieve(session, monster_id="mon-1", query="", k=4)

    # Assert: returns the canned recency rows, dict-shaped, in order.
    assert [d["id"] for d in out] == ["m1", "m2"]
    assert out[0]["summary"] == "newest"
    # Exactly one DB round-trip (the recency fallback), no hybrid search.
    assert session.execute_call_count == 1
    # Embedding was never requested for an empty query.
    assert stub_embed == []


async def test_whitespace_only_query_uses_recency_fallback(stub_embed) -> None:
    # Arrange: a whitespace-only query is treated as empty.
    session = _FakeSession([[_make_memory_row(mid="only")]])

    # Act
    out = await retrieve(session, monster_id="mon-1", query="   \t \n ", k=4)

    # Assert
    assert [d["id"] for d in out] == ["only"]
    assert session.execute_call_count == 1
    assert stub_embed == []


async def test_empty_query_with_string_event_type_filter_is_normalized(
    stub_embed,
) -> None:
    # Arrange: a lowercase string event_type must be coerced via EventType(upper()).
    session = _FakeSession([[_make_memory_row(mid="b1", event_type=EventType.battle)]])

    # Act: should not raise on the "battle" -> EventType.battle normalization path.
    out = await retrieve(
        session, monster_id="mon-1", query="", k=4, event_type="battle"
    )

    # Assert
    assert [d["id"] for d in out] == ["b1"]
    assert out[0]["event_type"] == "BATTLE"
    assert session.execute_call_count == 1


async def test_empty_query_with_invalid_event_type_string_does_not_raise(
    stub_embed,
) -> None:
    # Arrange: an unknown event_type string is swallowed (filter dropped), not raised.
    session = _FakeSession([[_make_memory_row(mid="x1")]])

    # Act
    out = await retrieve(
        session, monster_id="mon-1", query="", k=4, event_type="not-a-real-type"
    )

    # Assert: still returns the recency batch.
    assert [d["id"] for d in out] == ["x1"]


# --------------------------------------------------------------------------- #
# retriever.retrieve — RRF merge logic
# --------------------------------------------------------------------------- #


async def test_rrf_merge_boosts_item_present_in_both_result_sets(
    stub_embed,
) -> None:
    # Arrange:
    #   Vector search returns:  [shared, vonly]   (ranks 1, 2)
    #   Keyword search returns: [shared, konly]   (ranks 1, 2)
    # "shared" appears rank-1 in BOTH lists, so its RRF score is the sum of two
    # 1/(K+1) contributions and must rank first.
    shared = _make_memory_row(mid="shared", summary="shared")
    vonly = _make_memory_row(mid="vonly", summary="vector-only")
    konly = _make_memory_row(mid="konly", summary="keyword-only")

    vector_batch = [shared, vonly]
    keyword_batch = [shared, konly]
    session = _FakeSession([vector_batch, keyword_batch])

    # Act: a non-empty query with >=3-char tokens triggers both branches.
    out = await retrieve(
        session, monster_id="mon-1", query="logic evidence", k=4
    )

    ids = [d["id"] for d in out]

    # Assert: shared is first (fused from both rankings).
    assert ids[0] == "shared"
    # All three distinct ids are present, deduped.
    assert set(ids) == {"shared", "vonly", "konly"}
    assert len(ids) == 3
    # Two hybrid queries ran (vector + keyword), no recency fallback needed.
    assert session.execute_call_count == 2
    # embed() was invoked once for the query vector.
    assert stub_embed == [["logic evidence"]]


async def test_rrf_scores_match_reciprocal_rank_formula(stub_embed) -> None:
    # Arrange: disjoint result sets so each id's score is a single 1/(K+rank).
    #   vector:  [a (rank1), b (rank2)]
    #   keyword: [c (rank1), d (rank2)]
    a = _make_memory_row(mid="a")
    b = _make_memory_row(mid="b")
    c = _make_memory_row(mid="c")
    d = _make_memory_row(mid="d")
    session = _FakeSession([[a, b], [c, d]])

    # Act
    out = await retrieve(session, monster_id="mon-1", query="alpha beta", k=4)

    ids = [r["id"] for r in out]

    # Assert: rank-1 items (a from vector, c from keyword) tie at 1/(K+1) and must
    # both precede the rank-2 items (b, d) which tie at 1/(K+2).
    expected_rank1_score = 1.0 / (_RRF_K + 1)
    expected_rank2_score = 1.0 / (_RRF_K + 2)
    assert expected_rank1_score > expected_rank2_score
    # First two are the rank-1 winners, last two are the rank-2 items.
    assert set(ids[:2]) == {"a", "c"}
    assert set(ids[2:]) == {"b", "d"}


async def test_rrf_respects_k_limit_on_returned_results(stub_embed) -> None:
    # Arrange: five distinct vector hits, but k=2 must cap the output.
    vrows = [_make_memory_row(mid=f"v{i}") for i in range(5)]
    session = _FakeSession([vrows, []])  # vector batch, empty keyword batch

    # Act
    out = await retrieve(session, monster_id="mon-1", query="some query", k=2)

    # Assert: only top-k returned, best-first (lowest rank = highest RRF score).
    assert [d["id"] for d in out] == ["v0", "v1"]
    assert len(out) == 2


async def test_no_hybrid_hits_falls_back_to_recency(stub_embed) -> None:
    # Arrange: non-empty query, but BOTH vector and keyword searches return
    # nothing -> ranked_ids is empty -> a third (recency) query runs.
    recency_rows = [_make_memory_row(mid="recent1"), _make_memory_row(mid="recent2")]
    session = _FakeSession([[], [], recency_rows])

    # Act
    out = await retrieve(session, monster_id="mon-1", query="orphan query", k=4)

    # Assert: recency fallback rows are returned.
    assert [d["id"] for d in out] == ["recent1", "recent2"]
    # Three queries: vector (empty), keyword (empty), recency fallback.
    assert session.execute_call_count == 3


async def test_short_token_query_skips_keyword_branch(stub_embed) -> None:
    # Arrange: query has only <3-char tokens, so kw_tokens is empty and the
    # keyword SELECT is skipped. Vector branch still runs (1 execute), then the
    # keyword execute is NOT issued.
    vrows = [_make_memory_row(mid="v-hit")]
    session = _FakeSession([vrows])  # only the vector query consumes a batch

    # Act: "a", "of" are both <3 chars after stripping.
    out = await retrieve(session, monster_id="mon-1", query="a of", k=4)

    # Assert: vector hit returned via RRF; keyword query never executed.
    assert [d["id"] for d in out] == ["v-hit"]
    assert session.execute_call_count == 1


async def test_memory_dict_shape_contains_expected_fields(stub_embed) -> None:
    # Arrange
    row = _make_memory_row(
        mid="shape-1",
        event_type=EventType.player,
        summary="a summary",
        content="some content",
        salience=0.9,
        created_at=_dt.datetime(2026, 6, 20, 8, 30, 0),
    )
    session = _FakeSession([[row], []])

    # Act
    out = await retrieve(session, monster_id="mon-1", query="hello world", k=4)

    # Assert: dict carries the MemoryItem-shaped fields with serialized values.
    item = out[0]
    assert item["id"] == "shape-1"
    assert item["event_type"] == "PLAYER"  # enum .value
    assert item["summary"] == "a summary"
    assert item["content"] == "some content"
    assert item["salience"] == 0.9
    assert item["created_at"] == "2026-06-20T08:30:00"  # isoformat
