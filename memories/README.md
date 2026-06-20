# memories/

In-repo, version-controlled **agent memory log**. This is how parallel subagents
and future sessions hand off context without re-deriving it.

## Convention
- Each workstream/agent appends to its own file: `memories/<workstream>.md`
  (e.g. `ws-b-debate.md`). One concern per file.
- `MEMORY.md` is the index — one line per memory file, loaded first to decide what's relevant.
- When you finish a chunk of work, record:
  - **Owns:** files/dirs you created or are responsible for.
  - **Decisions:** non-obvious choices and *why* (not what the code already says).
  - **Interfaces:** functions/types you exposed for others, or stubs you consumed.
  - **Open TODOs:** what's unfinished or needs Wave 2 integration.
- Link related memories with `[[ws-x-name]]`.

Keep entries short and factual. Do not duplicate what the code or git history records.
