/**
 * QuestLogScreen (WS-2, issue #7) — the body of the "quests" Adventure-menu
 * overlay. Lists the run's quests via GET /api/runs/{id}/quests.
 *
 * Rendered inside OverlayHost's <MenuPanel> frame, so this paints only the BODY
 * (the four state-matrix states from UI_CONTRACT.md §State matrix). The minimap
 * pin for a quest's target_xy is the OVERWORLD owner's job — this is just the log.
 * Completed / failed quests render disabled (visible but not selectable); active
 * quests stay selectable (selecting one just re-affirms it / no-op for now).
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import { ListMenu, LoadingState, EmptyState, ErrorState } from "./shell";
import type { ListMenuItem } from "./shell";
import { INTRO_SCRIPT } from "../content/introScript";

interface Quest {
  id: string;
  type: string;
  title: string;
  description: string;
  status: string;
  target_xy: { x: number; y: number } | null;
  reward: unknown;
}

interface QuestsResponse {
  quests: Quest[];
}

const STATUS_GLYPH: Record<string, string> = {
  active: "▸",
  in_progress: "▸",
  completed: "✓",
  complete: "✓",
  done: "✓",
  failed: "✗",
  locked: "🔒",
};

function isClosed(status: string): boolean {
  const s = status.toLowerCase();
  return s === "completed" || s === "complete" || s === "done" || s === "failed" || s === "locked";
}

export default function QuestLogScreen() {
  const runId = useGame((s) => s.runId);
  const [quests, setQuests] = useState<Quest[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchQuests = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.get<QuestsResponse>(`/api/runs/${runId}/quests`);
      setQuests(res.quests ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to read the log.");
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void fetchQuests();
  }, [fetchQuests]);

  if (loading && quests === null) return <LoadingState label="Consulting the log" />;
  if (error)
    return (
      <ErrorState message="The log is illegible." detail={error} onRetry={() => void fetchQuests()} />
    );
  if (!quests || quests.length === 0)
    return (
      <EmptyState
        icon="📜"
        title="No quests yet"
        message={`Talk to NPCs to take on quests. Your first comes from ${INTRO_SCRIPT.npcName}.`}
      />
    );

  const rows: ListMenuItem<Quest>[] = quests.map((q) => {
    const glyph = STATUS_GLYPH[q.status.toLowerCase()] ?? "•";
    return {
      id: q.id,
      label: `${glyph} ${q.title}`,
      trailing: q.type,
      hint: q.description,
      disabled: isClosed(q.status),
      value: q,
    };
  });

  return (
    <div className="space-y-2">
      <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
        Active quests are highlighted; completed ones are dimmed.
      </div>
      <ListMenu items={rows} ariaLabel="Quest log" onSelect={() => {}} />
    </div>
  );
}
