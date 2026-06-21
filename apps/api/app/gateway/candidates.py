"""candidates.py — config-gated failover routing across a candidate model chain.

WS-0-LAT: turn ``settings.gateway_actor_candidates`` / ``gateway_judge_candidates``
(comma lists like ``groq/llama-3.1-8b-instant,...,ollama/gemma3:1b``) from inert
config into a real runner. A spec resolves to the right backend:

  * ``ollama/<model>``         -> the local LLMGateway (app.gateway.gateway).
  * ``anthropic/<model>``,
    ``openai/<model>``          -> the local LLMGateway (it already routes these).
  * ``groq/<model>``, ``cerebras/<model>``, ``gemini/<model>``,
    ``openrouter/<model>``      -> the existing HOSTED failover adapter
                                   (app.llm.hosted_adapter), reused, not reinvented.
  * a bare name (no ``/``)     -> the local gateway on the default provider.

DEMO-CRITICAL — battles stay LOCAL by default
---------------------------------------------
The candidate chain is OPT-IN. The live battle path (orchestrator) does NOT call
this resolver for normal skill turns — it keeps calling ``gateway.complete`` /
``gateway.stream`` against the local fast model. This runner is the explicit,
config-gated switch the go/no-go uses to A/B local vs hosted, and the seam an
INFREQUENT task can choose to route through. Two gates BOTH must hold for a hosted
candidate to ever run:

  1. ``settings.gateway_fallback_enabled`` is True, AND
  2. the caller explicitly invokes ``run_candidates(...)`` / ``run_candidate_chain``
     (nothing in the hot battle loop does).

When fallback is disabled, ``run_candidates`` collapses to the FIRST candidate
only (no failover) — so even an opt-in caller can't silently fan out to hosted
providers unless fallback is on.

Public surface
--------------
    parse_candidates(spec_csv) -> list[Candidate]
    resolve_candidate(spec)    -> Candidate            # which backend a spec uses
    run_candidate(candidate, messages, ...) -> CandidateResult
    run_candidates(specs, messages, ...)    -> CandidateResult   # first success
    actor_candidates() / judge_candidates() -> list[Candidate]   # from settings

Every call is best-effort and returns a structured result (never raises for an
expected provider failure), so a go/no-go run degrades gracefully when Ollama or
hosted keys are absent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from app.config import settings

# Backend kinds a candidate resolves to.
LOCAL_PROVIDERS = {"ollama", "anthropic", "openai"}
HOSTED_PROVIDERS = {"groq", "cerebras", "gemini", "openrouter"}

Message = dict[str, str]


@dataclass(frozen=True)
class Candidate:
    """A parsed candidate spec, e.g. ``groq/llama-3.1-8b-instant``.

    ``backend`` is "local" (route via LLMGateway) or "hosted" (route via the
    hosted_adapter failover). ``provider`` is the concrete provider token, and
    ``model`` is the model id passed to that backend.
    """

    spec: str
    provider: str
    model: str
    backend: str  # "local" | "hosted"


@dataclass
class CandidateResult:
    """Outcome of running one or more candidates."""

    text: str = ""
    ok: bool = False
    candidate: Optional[Candidate] = None  # the candidate that produced `text`
    latency_ms: int = 0
    error: str = ""
    attempts: int = 0  # how many candidates were tried


def resolve_candidate(spec: str) -> Candidate:
    """Resolve a single ``provider/model`` (or bare) spec to a Candidate.

    Mirrors ``app.gateway.models.resolve`` for the local providers and recognizes
    the hosted free-tier providers, so a chain mixing both routes correctly.
    """
    raw = (spec or "").strip()
    if "/" in raw:
        provider, _, model = raw.partition("/")
        provider = provider.strip().lower()
        model = model.strip()
        if provider in HOSTED_PROVIDERS:
            return Candidate(spec=raw, provider=provider, model=model, backend="hosted")
        if provider in LOCAL_PROVIDERS:
            return Candidate(spec=raw, provider=provider, model=model, backend="local")
        # Unknown provider prefix: treat the WHOLE spec as a model id on the
        # default local provider (matches gateway.resolve's bare-name fallback,
        # which would otherwise mis-split a model id that itself contains a '/').
        return Candidate(
            spec=raw, provider=settings.llm_provider, model=raw, backend="local"
        )
    # Bare model name -> default local provider.
    return Candidate(
        spec=raw, provider=settings.llm_provider, model=raw, backend="local"
    )


def parse_candidates(spec_csv: str | None) -> list[Candidate]:
    """Parse a comma-separated candidate list into Candidates (order preserved)."""
    if not spec_csv:
        return []
    out: list[Candidate] = []
    for part in spec_csv.split(","):
        part = part.strip()
        if part:
            out.append(resolve_candidate(part))
    return out


def actor_candidates() -> list[Candidate]:
    """The configured actor failover chain (``settings.gateway_actor_candidates``)."""
    return parse_candidates(settings.gateway_actor_candidates)


def judge_candidates() -> list[Candidate]:
    """The configured judge failover chain (``settings.gateway_judge_candidates``)."""
    return parse_candidates(settings.gateway_judge_candidates)


async def run_candidate(
    candidate: Candidate,
    messages: list[Message],
    *,
    temperature: float = 0.7,
    max_tokens: int = 256,
    timeout: float | None = None,
) -> CandidateResult:
    """Run ONE candidate against its resolved backend.

    Returns a structured CandidateResult; an expected provider failure (no key,
    timeout, HTTP error) yields ``ok=False`` rather than raising, so a chain can
    move on. Local candidates go through the singleton ``gateway``; hosted ones go
    through the ``hosted_adapter`` (flattening system/user messages to its
    prompt+system shape).
    """
    t0 = time.monotonic()
    try:
        if candidate.backend == "hosted":
            text = await _run_hosted(
                candidate, messages, max_tokens=max_tokens, temperature=temperature
            )
        else:
            # Local: route the resolved provider/model string through the gateway,
            # which already understands ``ollama/`` ``anthropic/`` ``openai/``.
            from app.gateway.gateway import gateway

            model_arg = (
                f"{candidate.provider}/{candidate.model}"
                if candidate.provider in LOCAL_PROVIDERS
                else candidate.model
            )
            text = await gateway.complete(
                messages,
                model=model_arg,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        text = (text or "").strip()
        latency_ms = int(round((time.monotonic() - t0) * 1000))
        return CandidateResult(
            text=text,
            ok=bool(text),
            candidate=candidate,
            latency_ms=latency_ms,
            attempts=1,
        )
    except Exception as exc:  # noqa: BLE001 — a candidate failing is expected; move on
        latency_ms = int(round((time.monotonic() - t0) * 1000))
        return CandidateResult(
            ok=False,
            candidate=candidate,
            latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}",
            attempts=1,
        )


async def _run_hosted(
    candidate: Candidate,
    messages: list[Message],
    *,
    max_tokens: int,
    temperature: float,
) -> str:
    """Route a hosted candidate through the existing hosted_adapter failover.

    The hosted adapter takes a single ``provider`` order + prompt/system; we pin
    its order to JUST this candidate's provider so a ``groq/...`` spec hits Groq
    (not the adapter's default fastest-first chain), preserving the candidate
    list's explicit priority. Returns "" when the provider has no key / fails so
    the chain falls through to the next candidate.
    """
    from app.llm.hosted_adapter import STUB_RESPONSE, HostedAdapter

    system = " ".join(m["content"] for m in messages if m.get("role") == "system")
    user = "\n".join(
        m["content"] for m in messages if m.get("role") != "system"
    ) or system

    adapter = HostedAdapter(order=(candidate.provider,))
    text = await adapter.complete(
        user,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system or None,
    )
    # The adapter returns its STUB when the provider has no key / failed — treat
    # that as a miss so the candidate chain can fall through.
    if text == STUB_RESPONSE:
        return ""
    return text


async def run_candidates(
    specs: list[str] | list[Candidate] | None,
    messages: list[Message],
    *,
    temperature: float = 0.7,
    max_tokens: int = 256,
    timeout: float | None = None,
) -> CandidateResult:
    """Run a candidate chain, returning the FIRST success.

    Fallback gating (demo-critical):
      * If ``settings.gateway_fallback_enabled`` is False, ONLY the first
        candidate is attempted (no failover fan-out) — so an opt-in caller can't
        silently spray hosted providers when fallback is off.
      * If True, candidates are tried in order until one returns non-empty text.

    Returns a CandidateResult whose ``attempts`` reflects how many were tried and
    whose ``candidate`` is the one that produced ``text`` (or the last tried on
    total failure). Never raises for expected provider failures.
    """
    cands: list[Candidate] = []
    for s in specs or []:
        cands.append(s if isinstance(s, Candidate) else resolve_candidate(s))
    if not cands:
        return CandidateResult(ok=False, error="no candidates", attempts=0)

    if not getattr(settings, "gateway_fallback_enabled", True):
        # No failover: first candidate only.
        cands = cands[:1]

    last: CandidateResult = CandidateResult(ok=False, attempts=0)
    attempts = 0
    for cand in cands:
        res = await run_candidate(
            cand,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        attempts += 1
        res.attempts = attempts
        if res.ok:
            return res
        last = res
    last.attempts = attempts
    return last
