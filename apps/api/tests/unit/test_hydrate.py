"""Unit tests for Wave A's background hydration (app/party/hydrate.py).

Covers:
  * `_summary_url(...)` rewrites a /wiki/<title> URL to the REST summary URL.
  * `_persona_key_from_url(...)` produces a stable cache slug.
  * disk cache round-trip writes a json file under a temporary cache dir.
  * `hydrate_monster(...)` happy path: stubbed Wikipedia + stubbed gateway both
    succeed -> distilled blob is written to the cache and patched onto the
    monster (`wiki_hydrated=True`, `voice` populated).
  * `hydrate_monster(...)` fallback: wiki fetch fails -> the patch still happens
    with the seed tagline as `voice` and `wiki_hydrated=True` (so the FE poll
    exits its waiting state).

Everything is in-process. The patch path is intercepted with a monkeypatched
`_patch_monster` so no DB is touched. The Wikipedia fetch path is intercepted
with a fake `httpx` module installed via monkeypatch.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, Optional

import pytest

hydrate = pytest.importorskip("app.party.hydrate")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _isolated_cache(tmp_path, monkeypatch) -> None:
    """Redirect hydrate's disk cache into a per-test tmp directory."""
    cache_dir = tmp_path / "personas_cache"
    monkeypatch.setattr(hydrate, "_CACHE_DIR", cache_dir)


class _FakeMonster:
    """In-memory stand-in for app.db.models.Monster."""

    def __init__(self) -> None:
        self.id = "mon-1"
        self.persona: dict[str, Any] = {"key": "socrates", "tagline": "seed tag"}
        self.wiki_hydrated = False
        self.genome_version = 1


def _install_patch_capture(monkeypatch, captured: dict[str, Any]) -> _FakeMonster:
    """Replace `_patch_monster` so we can assert on what would have been written."""
    monster = _FakeMonster()

    async def fake_patch(monster_id: str, persona_blob: dict[str, Any]) -> None:
        captured["monster_id"] = monster_id
        captured["persona_blob"] = persona_blob
        monster.persona.update(persona_blob)
        monster.wiki_hydrated = True
        monster.genome_version += 1

    monkeypatch.setattr(hydrate, "_patch_monster", fake_patch)
    captured["monster"] = monster
    return monster


def _install_fake_httpx(monkeypatch, *, status: int = 200, body: Optional[dict] = None,
                       raise_on_get: bool = False) -> None:
    """Provide a fake `httpx` module the hydrate fetch path can import."""

    class _Resp:
        def __init__(self, status_code: int, payload: Optional[dict]) -> None:
            self.status_code = status_code
            self._payload = payload or {}

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def get(self, *_a: Any, **_k: Any) -> _Resp:
            if raise_on_get:
                raise RuntimeError("network down")
            return _Resp(status, body)

    fake = types.SimpleNamespace(AsyncClient=_Client)
    monkeypatch.setitem(sys.modules, "httpx", fake)  # type: ignore[arg-type]


def _install_fake_gateway(monkeypatch, *, distilled_json: dict[str, Any]) -> dict[str, Any]:
    """Replace the gateway singleton with a stub that returns `distilled_json`."""
    calls: dict[str, Any] = {"count": 0, "last_messages": None}

    class _StubGateway:
        async def complete(
            self,
            messages: list[dict[str, str]],
            model: Optional[str] = None,
            temperature: float = 0.7,
            max_tokens: int = 512,
            json_mode: bool = False,
            timeout: Optional[float] = None,
        ) -> str:
            calls["count"] += 1
            calls["last_messages"] = messages
            calls["model"] = model
            calls["json_mode"] = json_mode
            return json.dumps(distilled_json)

    stub = _StubGateway()

    fake_mod = types.SimpleNamespace(gateway=stub)
    monkeypatch.setitem(sys.modules, "app.gateway.gateway", fake_mod)  # type: ignore[arg-type]
    return calls


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_summary_url_rewrites_wiki_page_to_rest_endpoint() -> None:
    out = hydrate._summary_url("https://en.wikipedia.org/wiki/Socrates")
    assert out == "https://en.wikipedia.org/api/rest_v1/page/summary/Socrates"


def test_summary_url_returns_none_for_non_wiki_input() -> None:
    assert hydrate._summary_url("") is None
    assert hydrate._summary_url("https://example.com/about") is None


def test_persona_key_from_url_slugifies_title() -> None:
    assert (
        hydrate._persona_key_from_url("https://en.wikipedia.org/wiki/Linus_Torvalds")
        == "linus_torvalds"
    )
    # Bad inputs -> None.
    assert hydrate._persona_key_from_url("") is None
    assert hydrate._persona_key_from_url("not a url") is None


def test_safe_parse_json_recovers_first_object_from_messy_blob() -> None:
    out = hydrate._safe_parse_json('garbage prefix {"voice": "hi"} trailing junk')
    assert out == {"voice": "hi"}


def test_ensure_shape_fills_missing_keys_with_fallback_tagline() -> None:
    shaped = hydrate._ensure_shape({}, "fallback")
    assert shaped == {
        "voice": "fallback",
        "views": [],
        "quotes": [],
        "domain_keywords": [],
    }


# --------------------------------------------------------------------------- #
# Disk cache round-trip
# --------------------------------------------------------------------------- #


def test_cache_write_and_read_round_trip(tmp_path, monkeypatch) -> None:
    _isolated_cache(tmp_path, monkeypatch)
    payload = {"voice": "v", "views": ["a"], "quotes": [], "domain_keywords": []}
    hydrate._cache_write("socrates", payload)
    out = hydrate._cache_read("socrates")
    assert out == payload


def test_cache_read_returns_none_for_missing_key(tmp_path, monkeypatch) -> None:
    _isolated_cache(tmp_path, monkeypatch)
    assert hydrate._cache_read("nope") is None


# --------------------------------------------------------------------------- #
# hydrate_monster — happy path
# --------------------------------------------------------------------------- #


def test_hydrate_monster_happy_path_writes_cache_and_patches_monster(
    tmp_path, monkeypatch
) -> None:
    _isolated_cache(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}
    _install_patch_capture(monkeypatch, captured)
    _install_fake_httpx(
        monkeypatch,
        status=200,
        body={"description": "Greek philosopher", "extract": "Socrates was a classical Greek philosopher."},
    )
    gw_calls = _install_fake_gateway(
        monkeypatch,
        distilled_json={
            "voice": "I know that I know nothing.",
            "views": ["question everything", "examined life"],
            "quotes": ["The unexamined life is not worth living."],
            "domain_keywords": ["ethics", "logic", "philosophy"],
        },
    )

    asyncio.run(
        hydrate.hydrate_monster(
            monster_id="mon-1",
            wiki_url="https://en.wikipedia.org/wiki/Socrates",
            fallback_tagline="seed tag",
        )
    )

    # 1. Cache written.
    cached = hydrate._cache_read("socrates")
    assert cached is not None
    assert cached["voice"].startswith("I know")
    assert "ethics" in cached["domain_keywords"]

    # 2. Monster patched with the distilled blob; wiki_hydrated flipped.
    assert captured["monster_id"] == "mon-1"
    assert captured["persona_blob"]["voice"].startswith("I know")
    monster = captured["monster"]
    assert monster.wiki_hydrated is True
    assert monster.persona["voice"].startswith("I know")

    # 3. Gateway was called exactly once with JSON mode on.
    assert gw_calls["count"] == 1
    assert gw_calls["json_mode"] is True


# --------------------------------------------------------------------------- #
# hydrate_monster — fallback path
# --------------------------------------------------------------------------- #


def test_hydrate_monster_fallback_when_wiki_unreachable(
    tmp_path, monkeypatch
) -> None:
    _isolated_cache(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}
    _install_patch_capture(monkeypatch, captured)
    # Httpx import works but the GET raises → wiki_text is "".
    _install_fake_httpx(monkeypatch, raise_on_get=True)
    # Gateway is stubbed but should never be called (no wiki text to distill).
    gw_calls = _install_fake_gateway(monkeypatch, distilled_json={"voice": "won't run"})

    asyncio.run(
        hydrate.hydrate_monster(
            monster_id="mon-1",
            wiki_url="https://en.wikipedia.org/wiki/Socrates",
            fallback_tagline="I know that I know nothing.",
        )
    )

    # Patch still happened — wiki_hydrated MUST flip True so the FE poll exits.
    assert captured["monster_id"] == "mon-1"
    monster = captured["monster"]
    assert monster.wiki_hydrated is True

    # Fallback tagline ended up as the voice on the patched blob.
    blob = captured["persona_blob"]
    assert blob["voice"] == "I know that I know nothing."
    assert blob["views"] == []
    assert blob["quotes"] == []
    assert blob["domain_keywords"] == []

    # Gateway distill is skipped when there's no wiki text.
    assert gw_calls["count"] == 0

    # And the cache is untouched (no successful fetch).
    assert hydrate._cache_read("socrates") is None


def test_hydrate_monster_uses_cache_when_available(tmp_path, monkeypatch) -> None:
    _isolated_cache(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}
    _install_patch_capture(monkeypatch, captured)

    # Pre-seed the disk cache.
    cached = {
        "voice": "cached voice",
        "views": ["v1"],
        "quotes": ["q1"],
        "domain_keywords": ["k1"],
    }
    hydrate._cache_write("socrates", cached)

    # No httpx / gateway stubs needed — the cache hit short-circuits both.
    asyncio.run(
        hydrate.hydrate_monster(
            monster_id="mon-1",
            wiki_url="https://en.wikipedia.org/wiki/Socrates",
            fallback_tagline="ignored",
        )
    )

    assert captured["persona_blob"]["voice"] == "cached voice"
    assert captured["monster"].wiki_hydrated is True
