/**
 * NPCDialogue (WS-3, issues #13 + #5; living-world LLM NPCs) — the overworld NPC
 * talk surface, now a multi-turn conversation drawer. Presents as a HUD drawer
 * anchored to the bottom of the overworld (z-40), per UI_CONTRACT.md
 * §Presentation.
 *
 * Two modes:
 *  1. Scripted intro / onboarding (issue #5). When the NPC is the intro
 *     quest-giver (content/introScript.ts), it plays INTRO_SCRIPT.lines one at a
 *     time, then renders INTRO_SCRIPT.choices via <ListMenu>. The
 *     "accept_quest_and_pull" choice grants the first quest
 *     (POST onboarding/first-quest) and triggers the first pull
 *     (POST onboarding/first-pull) — the EXISTING gacha funnel, not a competing
 *     one — via the optional `onOnboarded` hook so App can leave the gacha gate.
 *  2. Generic talk (issue #13 + living-world). For other NPCs it opens a
 *     multi-turn LLM conversation: the first POST seeds a greeting, and the
 *     player can type follow-up messages that POST to
 *     /api/runs/{runId}/npc/{npc_id}/talk with a conversation_id so the server
 *     keeps Redis-backed history. Merchants / innkeepers also offer a diegetic
 *     action choice (Browse wares → openShop; Make camp → openCamp), and
 *     merchants / quest_givers can hand out a quest (POST quest/accept).
 *
 * Self-contained (fetch on mount, clean up on unmount). It deliberately does NOT
 * import App; the onboarding handoff is via the optional `onOnboarded` callback
 * so the dialogue can be reused both in the overworld and on the gacha gate.
 */
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
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

interface TalkTurn {
  role: "player" | "npc";
  text: string;
}

interface TalkResponse {
  npc_id: string;
  name: string;
  archetype: string;
  text: string;
  cached: boolean;
  conversation_id?: string | null;
  history?: TalkTurn[];
}

interface QuestResponse {
  quest: {
    title: string;
    description: string;
    reward: string;
    target: string;
  } | null;
}

interface QuestResponse {
  quest: {
    title: string;
    description: string;
    reward: string;
    target: string;
  } | null;
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
// Generic talk dialogue (#13 + living-world) — multi-turn LLM conversation with
// optional diegetic action choice and quest offer.
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
  const [messages, setMessages] = useState<TalkTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [quest, setQuest] = useState<QuestResponse["quest"]>(null);
  const [questLoading, setQuestLoading] = useState(false);
  const [questError, setQuestError] = useState<string | null>(null);

  // The diegetic action this NPC offers, by archetype.
  const action: GenericAction = useMemo(() => {
    if (npc.archetype === "merchant") return { kind: "shop" };
    if (npc.archetype === "innkeeper") return { kind: "camp" };
    return null;
  }, [npc.archetype]);

  const canOfferQuest =
    npc.archetype === "merchant" || npc.archetype === "quest_giver";

  // Open the conversation: seed a greeting (and any persisted history) from the
  // talk endpoint, opening a fresh conversation_id.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const initialConversationId = `${npc.npc_id}-${Date.now().toString(36)}`;
    setLoading(true);
    setError(null);
    setMessages([]);
    setDraft("");
    setConversationId(initialConversationId);
    setQuest(null);
    setQuestError(null);
    setQuestLoading(false);

    api
      .post<TalkResponse>(`/api/runs/${runId}/npc/${npc.npc_id}/talk`, {
        conversation_id: initialConversationId,
      })
      .then((res) => {
        if (cancelled) return;
        setConversationId(res.conversation_id ?? initialConversationId);
        setMessages(res.history?.length ? res.history : [{ role: "npc", text: res.text }]);
      })
      .catch(() => {
        if (cancelled) return;
        // Fall back to the canned greeting for shop/camp NPCs so the action is
        // still reachable even if the talk endpoint is unavailable.
        if (action?.kind === "shop")
          setMessages([{ role: "npc", text: MERCHANT_DIALOGUE.greeting }]);
        else if (action?.kind === "camp")
          setMessages([{ role: "npc", text: INNKEEPER_DIALOGUE.greeting }]);
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

  const sendMessage = (event: FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (!runId || !npc || !text || loading) return;
    const nextConversationId =
      conversationId ?? `${npc.npc_id}-${Date.now().toString(36)}`;

    setDraft("");
    setError(null);
    setLoading(true);
    setConversationId(nextConversationId);
    setMessages((current) => [...current, { role: "player", text }]);

    api
      .post<TalkResponse>(`/api/runs/${runId}/npc/${npc.npc_id}/talk`, {
        message: text,
        conversation_id: nextConversationId,
      })
      .then((res) => {
        setConversationId(res.conversation_id ?? nextConversationId);
        if (res.history?.length) {
          setMessages(res.history);
        } else {
          setMessages((current) => [...current, { role: "npc", text: res.text }]);
        }
      })
      .catch(() => setError("The path is silent."))
      .finally(() => setLoading(false));
  };

  const acceptQuest = () => {
    if (!runId || !npc || questLoading) return;
    setQuestLoading(true);
    setQuestError(null);
    api
      .post<QuestResponse>(`/api/runs/${runId}/quest/accept`, { npc_id: npc.npc_id })
      .then((res) => {
        setQuest(res.quest);
        if (!res.quest) setQuestError("No work nearby.");
      })
      .catch(() => setQuestError("The notice board is blank."))
      .finally(() => setQuestLoading(false));
  };

  // Diegetic action rows (shop / camp / leave) shown beneath the conversation.
  const rows: ListMenuItem<"shop" | "camp" | "leave">[] = [];
  if (action?.kind === "shop")
    rows.push({ id: "shop", label: MERCHANT_DIALOGUE.actionLabel, value: "shop" });
  if (action?.kind === "camp")
    rows.push({ id: "camp", label: INNKEEPER_DIALOGUE.actionLabel, value: "camp" });
  rows.push({ id: "leave", label: "Farewell.", value: "leave" });

  return (
    <DialogueDrawer name={npc.name || npc.npc_id} archetype={npc.archetype} onClose={onClose}>
      {error && messages.length === 0 ? (
        <ErrorState message="The path is silent." />
      ) : (
        <div className="space-y-2">
          {messages.length === 0 && loading ? (
            <p className="font-body text-sm min-h-10 leading-relaxed" style={{ color: "var(--ink)" }}>
              …
            </p>
          ) : null}
          {messages.length ? (
            <div className="space-y-2">
              {messages.map((message, index) => (
                <div
                  key={`${message.role}-${index}-${message.text.slice(0, 12)}`}
                  className={message.role === "player" ? "text-right" : "text-left"}
                >
                  <span
                    className="font-body text-sm leading-relaxed inline-block max-w-[92%]"
                    style={{
                      color: message.role === "player" ? "var(--accent)" : "var(--ink)",
                    }}
                  >
                    {message.text}
                  </span>
                </div>
              ))}
            </div>
          ) : null}

          <form className="mt-3 flex items-center gap-2" onSubmit={sendMessage}>
            <input
              className="pixel-field font-body text-sm flex-1 min-w-0"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              disabled={loading}
              maxLength={800}
              placeholder="Say something…"
            />
            <button
              className="pixel-btn pixel-btn--accent text-[9px] py-1"
              type="submit"
              disabled={loading || !draft.trim()}
            >
              {loading ? "…" : "Say"}
            </button>
          </form>

          {/* Diegetic action(s): Browse wares / Make camp / Farewell. */}
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

          {canOfferQuest ? (
            <div
              className="mt-3 border-t pt-3"
              style={{ borderColor: "rgba(232,230,216,0.16)" }}
            >
              {quest ? (
                <div className="font-body text-xs leading-relaxed" style={{ color: "var(--ink)" }}>
                  <div className="font-hud text-[9px] mb-1" style={{ color: "var(--accent)" }}>
                    {quest.title}
                  </div>
                  <div>{quest.description}</div>
                  <div className="mt-1" style={{ color: "var(--muted)" }}>
                    Reward: {quest.reward}
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <button
                    className="pixel-btn pixel-btn--accent text-[9px] py-1"
                    onClick={acceptQuest}
                    disabled={questLoading}
                  >
                    {questLoading ? "Checking…" : "Take quest"}
                  </button>
                  {questError ? (
                    <span className="font-body text-xs" style={{ color: "var(--muted)" }}>
                      {questError}
                    </span>
                  ) : null}
                </div>
              )}
            </div>
          ) : null}
          {canOfferQuest ? (
            <div
              className="mt-3 border-t pt-3"
              style={{ borderColor: "rgba(232,230,216,0.16)" }}
            >
              {quest ? (
                <div className="font-body text-xs leading-relaxed" style={{ color: "var(--ink)" }}>
                  <div className="font-hud text-[9px] mb-1" style={{ color: "var(--accent)" }}>
                    {quest.title}
                  </div>
                  <div>{quest.description}</div>
                  <div className="mt-1" style={{ color: "var(--muted)" }}>
                    Reward: {quest.reward}
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <button
                    className="pixel-btn pixel-btn--accent text-[9px] py-1"
                    onClick={acceptQuest}
                    disabled={questLoading}
                  >
                    {questLoading ? "Checking..." : "Take quest"}
                  </button>
                  {questError ? (
                    <span className="font-body text-xs" style={{ color: "var(--muted)" }}>
                      {questError}
                    </span>
                  ) : null}
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}
    </DialogueDrawer>
  );
}

export default NPCDialogue;
