/**
 * NPCDialogue (WS-3, issues #13 + #5) — the overworld NPC talk surface, now a
 * choice MENU (was a single floating line). Presents as a HUD drawer anchored to
 * the bottom of the overworld (z-40), per UI_CONTRACT.md §Presentation.
 *
 * Two modes:
 *  1. Scripted intro / onboarding (issue #5). When the NPC is the intro
 *     quest-giver (content/introScript.ts), it plays INTRO_SCRIPT.lines one at a
 *     time, then renders INTRO_SCRIPT.choices via <ListMenu>. The
 *     "accept_quest_and_pull" choice grants the first quest
 *     (POST onboarding/first-quest) and triggers the first pull
 *     (POST onboarding/first-pull) — the EXISTING gacha funnel, not a competing
 *     one — via the optional `onOnboarded` hook so App can leave the gacha gate.
 *  2. Generic talk (issue #13). For other NPCs it fetches the talk line and, for
 *     merchants / innkeepers, offers a diegetic action choice (Browse wares →
 *     openShop; Make camp → openCamp). Plain villagers just get a Close.
 *
 * Self-contained (fetch on mount, clean up on unmount). It deliberately does NOT
 * import App; the onboarding handoff is via the optional `onOnboarded` callback
 * so the dialogue can be reused both in the overworld and on the gacha gate.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import type { NPCAnchorView } from "./NPCBehavior";
import { ListMenu, ErrorState } from "../ui/shell";
import type { ListMenuItem } from "../ui/shell";
import {
  INTRO_SCRIPT,
  FIRST_QUEST_ID,
  MERCHANT_DIALOGUE,
  INNKEEPER_DIALOGUE,
  type IntroChoice,
} from "../content/introScript";

interface TalkResponse {
  npc_id: string;
  name: string;
  archetype: string;
  text: string;
  cached: boolean;
}

interface NPCDialogueProps {
  runId: string | null;
  npc: NPCAnchorView | null;
  onClose: () => void;
  /** Fired after the intro NPC's accept-and-pull onboarding succeeds, so the
   *  host (App) can clear the gacha gate and enter the overworld. */
  onOnboarded?: () => void;
}

type GenericAction = { kind: "shop" } | { kind: "camp" } | null;

export function NPCDialogue({ runId, npc, onClose, onOnboarded }: NPCDialogueProps) {
  const openShop = useGame((s) => s.openShop);
  const openCamp = useGame((s) => s.openCamp);

  // Is this the scripted intro NPC? If so we drive the onboarding script instead
  // of the generic talk endpoint.
  const isIntro = npc?.npc_id === INTRO_SCRIPT.npcId;

  if (!npc) return null;
  return isIntro ? (
    <IntroDialogue runId={runId} onClose={onClose} onOnboarded={onOnboarded} />
  ) : (
    <GenericDialogue
      runId={runId}
      npc={npc}
      onClose={onClose}
      openShop={openShop}
      openCamp={openCamp}
    />
  );
}

// ---------------------------------------------------------------------------
// Shared HUD drawer chrome (bottom-anchored, z-40, pointer-events scoped).
// ---------------------------------------------------------------------------

function DialogueDrawer({
  name,
  archetype,
  onClose,
  children,
}: {
  name: string;
  archetype?: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  // Esc closes the drawer; stopPropagation so it doesn't also drive Phaser.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="absolute inset-x-3 bottom-3 z-40 pointer-events-none">
      <div className="pixel-panel pointer-events-auto max-w-3xl mx-auto p-3">
        <div className="flex items-center gap-2 mb-2">
          <span className="font-hud text-[10px]" style={{ color: "var(--accent)" }}>
            {name}
          </span>
          {archetype ? (
            <span className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
              {archetype}
            </span>
          ) : null}
          <button className="pixel-btn text-[9px] py-0.5 ml-auto" onClick={onClose}>
            ✕ Close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Intro / onboarding dialogue (#5)
// ---------------------------------------------------------------------------

function IntroDialogue({
  runId,
  onClose,
  onOnboarded,
}: {
  runId: string | null;
  onClose: () => void;
  onOnboarded?: () => void;
}) {
  // Line cursor; when it passes the last line we show the choice list.
  const [lineIdx, setLineIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const lines = INTRO_SCRIPT.lines;
  const atChoices = lineIdx >= lines.length;
  const current = lines[Math.min(lineIdx, lines.length - 1)];

  const advance = useCallback(() => {
    setLineIdx((i) => Math.min(i + 1, lines.length));
  }, [lines.length]);

  // Click / Enter advances the lines (until the choices show).
  useEffect(() => {
    if (atChoices) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
        advance();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [atChoices, advance]);

  const onChoice = useCallback(
    async (choice: IntroChoice) => {
      if (choice.effect === "repeat_explanation") {
        // Re-show the explanation lines (the "reasoning is the weapon" beats).
        setLineIdx(1);
        return;
      }
      if (choice.effect === "decline") {
        onClose();
        return;
      }
      // accept_quest_and_pull — grant the first quest, then the first pull. This
      // is the EXISTING onboarding gacha funnel (onboarding/first-pull), NOT a
      // competing gacha/pull cinematic.
      if (!runId) return;
      setBusy(true);
      setError(null);
      try {
        await api.post(`/api/runs/${runId}/onboarding/first-quest`, {
          npc_id: INTRO_SCRIPT.npcId,
          objective: FIRST_QUEST_ID,
        });
        await api.post(`/api/runs/${runId}/onboarding/first-pull`, {});
        onOnboarded?.();
        onClose();
      } catch (e) {
        setError(e instanceof Error ? e.message : "The charm slipped from your grasp.");
      } finally {
        setBusy(false);
      }
    },
    [runId, onClose, onOnboarded]
  );

  const choiceRows: ListMenuItem<IntroChoice>[] = INTRO_SCRIPT.choices.map((c) => ({
    id: c.id,
    label: c.label,
    disabled: busy,
    value: c,
  }));

  return (
    <DialogueDrawer name={INTRO_SCRIPT.npcName} archetype={INTRO_SCRIPT.npcArchetype} onClose={onClose}>
      {error ? (
        <ErrorState message="The path is silent." detail={error} onRetry={() => setError(null)} />
      ) : !atChoices ? (
        <button
          type="button"
          className="text-left w-full"
          onClick={advance}
          aria-label="Continue"
        >
          <p
            className="font-body text-sm min-h-10 leading-relaxed"
            style={{ color: current.speaker === "narration" ? "var(--muted)" : "var(--ink)" }}
          >
            {current.speaker === "narration" ? <em>{current.text}</em> : current.text}
          </p>
          <p className="font-hud text-[9px] mt-1" style={{ color: "var(--muted)" }}>
            ▸ click / Enter to continue ({lineIdx + 1}/{lines.length})
          </p>
        </button>
      ) : (
        <div className="space-y-2">
          <p className="font-body text-sm" style={{ color: "var(--ink)" }}>
            {INTRO_SCRIPT.choicePrompt}
          </p>
          <ListMenu
            items={choiceRows}
            ariaLabel="Your reply"
            onSelect={(row) => row.value && onChoice(row.value)}
          />
          {busy ? (
            <p className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
              Summoning your first Speaker…
            </p>
          ) : null}
        </div>
      )}
    </DialogueDrawer>
  );
}

// ---------------------------------------------------------------------------
// Generic talk dialogue (#13) — line + optional diegetic action choice.
// ---------------------------------------------------------------------------

function GenericDialogue({
  runId,
  npc,
  onClose,
  openShop,
  openCamp,
}: {
  runId: string | null;
  npc: NPCAnchorView;
  onClose: () => void;
  openShop: (npcId: string) => void;
  openCamp: () => void;
}) {
  const [line, setLine] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The diegetic action this NPC offers, by archetype.
  const action: GenericAction = useMemo(() => {
    if (npc.archetype === "merchant") return { kind: "shop" };
    if (npc.archetype === "innkeeper") return { kind: "camp" };
    return null;
  }, [npc.archetype]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setLine("");

    api
      .post<TalkResponse>(`/api/runs/${runId}/npc/${npc.npc_id}/talk`)
      .then((res) => {
        if (cancelled) return;
        setLine(res.text);
      })
      .catch(() => {
        if (cancelled) return;
        // Fall back to the canned greeting for shop/camp NPCs so the action is
        // still reachable even if the talk endpoint is unavailable.
        if (action?.kind === "shop") setLine(MERCHANT_DIALOGUE.greeting);
        else if (action?.kind === "camp") setLine(INNKEEPER_DIALOGUE.greeting);
        else setError("The path is silent.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [runId, npc.npc_id, action]);

  // Record that the player talked to this NPC (best-effort; fire-and-forget).
  useEffect(() => {
    if (!runId) return;
    api.post(`/api/runs/${runId}/npc/${npc.npc_id}/debated`).catch(() => {});
  }, [runId, npc.npc_id]);

  const rows: ListMenuItem<"shop" | "camp" | "leave">[] = [];
  if (action?.kind === "shop")
    rows.push({ id: "shop", label: MERCHANT_DIALOGUE.actionLabel, value: "shop" });
  if (action?.kind === "camp")
    rows.push({ id: "camp", label: INNKEEPER_DIALOGUE.actionLabel, value: "camp" });
  rows.push({ id: "leave", label: "Farewell.", value: "leave" });

  return (
    <DialogueDrawer name={npc.name || npc.npc_id} archetype={npc.archetype} onClose={onClose}>
      {error ? (
        <ErrorState message="The path is silent." />
      ) : (
        <div className="space-y-2">
          <p className="font-body text-sm min-h-10 leading-relaxed" style={{ color: "var(--ink)" }}>
            {loading ? "…" : line}
          </p>
          {!loading ? (
            <ListMenu
              items={rows}
              ariaLabel="Your reply"
              onSelect={(row) => {
                if (row.value === "shop") {
                  openShop(npc.npc_id);
                  onClose();
                } else if (row.value === "camp") {
                  openCamp();
                  onClose();
                } else {
                  onClose();
                }
              }}
            />
          ) : null}
        </div>
      )}
    </DialogueDrawer>
  );
}

export default NPCDialogue;
