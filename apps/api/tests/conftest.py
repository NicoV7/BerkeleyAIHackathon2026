"""Shared pytest fixtures for the debate-rpg-api backend test suite.

Design goals (T0-BE / backend test infra):
  * Import-safe: importing this module must never touch a live Postgres, Redis,
    or model gateway. Every network-touching fixture is lazy and guarded.
  * Host-friendly collection: the live Docker stack may be mid-edit, so any
    DB-backed test is *skipped* (not errored) when Postgres is unreachable from
    the host. Collection therefore always passes on a bare host.
  * Deterministic LLM: the `gateway_mock` fixture monkeypatches the singleton
    gateway's complete/stream/embed to pure, deterministic stubs so debate /
    judge / memory logic can be exercised without Ollama/Anthropic/OpenAI.

Conventions:
  * Use the `db_available` / `require_db` fixtures to gate DB-backed tests.
  * Use `gateway_mock` to neutralize the model gateway.
  * Use `make_monster` / `make_run` factories for sample domain objects.
"""
from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any, Optional

import pytest


# --------------------------------------------------------------------------- #
# Postgres availability probe
# --------------------------------------------------------------------------- #
#
# The app's default DATABASE_URL points at the Docker-internal host "postgres",
# which does not resolve from the host running the tests. The compose file maps
# the container's 5432 to localhost:5432, so for *host* probing we rewrite the
# host/port to localhost before attempting a real connection. We never trust the
# URL alone — we attempt an actual asyncpg connect with a short timeout, because
# an open port is not proof of a ready database.

_DB_PROBE_TIMEOUT = 2.0


def _host_database_url() -> str:
    """Return the app's DATABASE_URL rewritten for host-side access.

    Docker-internal hostnames (`postgres`, `db`) are rewritten to `localhost`
    so the probe can reach the port the compose stack publishes.
    """
    from app.config import settings

    url = settings.database_url
    for docker_host in ("@postgres:", "@db:"):
        if docker_host in url:
            url = url.replace(docker_host, "@localhost:")
    return url


def _split_host_port(url: str) -> tuple[str, int]:
    """Extract (host, port) from a SQLAlchemy/asyncpg URL, defaulting to 5432."""
    # Strip scheme.
    after_scheme = url.split("://", 1)[-1]
    # Drop any credentials.
    netloc = after_scheme.split("@", 1)[-1]
    # netloc looks like host:port/dbname or host/dbname.
    host_port = netloc.split("/", 1)[0]
    if ":" in host_port:
        host, port_s = host_port.rsplit(":", 1)
        try:
            return host, int(port_s)
        except ValueError:
            return host, 5432
    return host_port, 5432


def _port_open(host: str, port: int, timeout: float) -> bool:
    """Cheap TCP reachability check before paying for a full DB handshake."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _async_db_reachable() -> bool:
    """Attempt a real (short-timeout) asyncpg connection to the host DB.

    Returns False on any failure — unreachable host, missing driver, auth error,
    or DB not yet accepting connections. Never raises.
    """
    url = _host_database_url()
    host, port = _split_host_port(url)

    # Fast negative path: don't bother with a driver handshake if the port is shut.
    if not _port_open(host, port, _DB_PROBE_TIMEOUT):
        return False

    try:
        import asyncpg  # noqa: PLC0415  (lazy: keep import-safe without the driver)
    except Exception:  # noqa: BLE001
        return False

    # asyncpg wants a plain DSN (no SQLAlchemy "+asyncpg" suffix) and the
    # localhost-rewritten host.
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn=dsn), timeout=_DB_PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001
        return False
    else:
        await conn.close()
        return True


# Cache the probe result for the whole session: it's the same answer every time
# and a real connect attempt is not free.
_DB_AVAILABLE_CACHE: Optional[bool] = None


def _db_available_cached() -> bool:
    global _DB_AVAILABLE_CACHE
    if _DB_AVAILABLE_CACHE is None:
        try:
            _DB_AVAILABLE_CACHE = asyncio.run(_async_db_reachable())
        except Exception:  # noqa: BLE001
            _DB_AVAILABLE_CACHE = False
    return _DB_AVAILABLE_CACHE


@pytest.fixture(scope="session")
def db_available() -> bool:
    """Session-scoped bool: is a Postgres reachable from the host right now?

    Use this to *conditionally* branch in a test. To hard-skip a DB-backed test,
    prefer the `require_db` fixture, which skips automatically.
    """
    return _db_available_cached()


@pytest.fixture
def require_db(db_available: bool) -> None:
    """Skip the requesting test unless a host-reachable Postgres is available.

    The live Docker stack may be mid-edit, so DB-backed tests must degrade to a
    skip (keeping host collection + CI-without-DB green) rather than error.
    """
    if not db_available:
        pytest.skip(
            "Postgres not reachable from host (DATABASE_URL); "
            "skipping DB-backed test. Bring up the compose stack to run it."
        )


# --------------------------------------------------------------------------- #
# Deterministic gateway mock
# --------------------------------------------------------------------------- #


class _StubGateway:
    """Deterministic stand-in for the LLM gateway's public surface.

    Records calls (so tests can assert on prompts) and returns stable outputs:
      * complete -> a deterministic string derived from the last user message.
      * stream   -> yields that string split into whitespace-preserving chunks.
      * embed    -> a fixed-dimension pseudo-embedding seeded by text length.
    """

    EMBED_DIM = 768

    def __init__(self) -> None:
        self.complete_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.embed_calls: list[dict[str, Any]] = []

    @staticmethod
    def _last_user(messages: list[dict[str, str]]) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                return m.get("content", "")
        return messages[-1].get("content", "") if messages else ""

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
        json_mode: bool = False,
    ) -> str:
        self.complete_calls.append(
            {"messages": messages, "model": model, "json_mode": json_mode}
        )
        if json_mode:
            # Plausible judge-style JSON so json_repair/parse paths stay exercised.
            return '{"score": 5, "rationale": "stub", "damage": 10}'
        return f"[stub:{model or 'default'}] {self._last_user(messages)}"

    async def stream(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"messages": messages, "model": model})
        text = f"[stub:{model or 'default'}] {self._last_user(messages)}"
        for token in text.split(" "):
            yield token + " "

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        self.embed_calls.append({"texts": texts, "model": model})
        out: list[list[float]] = []
        for t in texts:
            # Deterministic, normalized-ish vector; content-sensitive but stable.
            seed = (len(t) % 7) + 1
            out.append([(seed + i) % 10 / 10.0 for i in range(self.EMBED_DIM)])
        return out

    async def health(self) -> dict[str, Any]:
        return {"provider": "stub", "ok": True, "models": ["stub-model"]}

    async def aclose(self) -> None:  # parity with the real gateway
        return None


@pytest.fixture
def gateway_stub() -> _StubGateway:
    """A fresh deterministic gateway stub (not yet patched in)."""
    return _StubGateway()


@pytest.fixture
def gateway_mock(monkeypatch: pytest.MonkeyPatch, gateway_stub: _StubGateway) -> _StubGateway:
    """Monkeypatch the singleton gateway's methods to deterministic stubs.

    Patches `app.gateway.gateway.gateway.{complete,stream,embed}` in place so any
    module that did `from app.gateway.gateway import gateway` sees the stubs. The
    returned object records calls for assertions.
    """
    import app.gateway.gateway as gw_module

    monkeypatch.setattr(gw_module.gateway, "complete", gateway_stub.complete)
    monkeypatch.setattr(gw_module.gateway, "stream", gateway_stub.stream)
    monkeypatch.setattr(gw_module.gateway, "embed", gateway_stub.embed)
    monkeypatch.setattr(gw_module.gateway, "health", gateway_stub.health)
    return gateway_stub


# --------------------------------------------------------------------------- #
# Sample domain factories (monster / run)
# --------------------------------------------------------------------------- #


@pytest.fixture
def make_run():
    """Factory returning fresh `Run` ORM instances with sane defaults.

    Kwargs override any field. Instances are unsaved (no DB required) so they're
    usable in pure-logic tests; persist them yourself in a DB-backed test.
    """
    from app.db.models import Run, RunStatus

    def _make(**overrides: Any) -> Run:
        defaults: dict[str, Any] = {
            "debate_topic": "Should pineapple go on pizza?",
            "seed": 0,
            "player_x": 0,
            "player_y": 0,
            "status": RunStatus.active,
        }
        defaults.update(overrides)
        return Run(**defaults)

    return _make


@pytest.fixture
def make_monster(make_run):
    """Factory returning fresh `Monster` ORM instances with sane defaults.

    If no `run_id` is supplied, a transient `Run` is created and its id used, so
    the monster is self-consistent for pure-logic tests.
    """
    from app.db.models import DebateType, Monster, MonsterOwner

    def _make(**overrides: Any) -> Monster:
        run_id = overrides.pop("run_id", None)
        if run_id is None:
            run_id = make_run().id
        defaults: dict[str, Any] = {
            "run_id": run_id,
            "owner": MonsterOwner.player,
            "name": "Socratesaur",
            "type": DebateType.logos,
            "persona": {"tone": "measured", "tactics": ["evidence"]},
            "harness": {"system": "You argue with rigor."},
            "skills": [],
            "level": 1,
            "xp": 0,
            "max_hp": 100,
            "evolution_stage": 0,
        }
        defaults.update(overrides)
        return Monster(**defaults)

    return _make


@pytest.fixture
def sample_messages() -> list[dict[str, str]]:
    """A minimal, well-formed chat message list for gateway-style calls."""
    return [
        {"role": "system", "content": "You are a debate agent."},
        {"role": "user", "content": "Make your opening argument."},
    ]
