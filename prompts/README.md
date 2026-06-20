# prompts/

Canonical home for **plan files** and **per-workstream subagent briefs**.

- `ok-claude-use-toasty-pascal.md` — the approved master build plan (orchestrated, wave-based).
- `ws-*.md` — one brief per Wave 1 workstream (WS-A … WS-F). Each brief states the
  workstream's goal, the exact files it owns, the interfaces it must expose/consume,
  and its definition of done. Subagents read their brief before starting and must not
  edit files outside their ownership list.

When the orchestrator dispatches a wave, it writes the briefs here first so the
work is auditable and resumable.
