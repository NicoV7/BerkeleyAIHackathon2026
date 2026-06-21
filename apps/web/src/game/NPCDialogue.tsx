/**
 * NPCDialogue - overworld NPC talk surface.
 *
 * Intro NPCs use the scripted onboarding flow. All other NPCs open a short
 * multi-turn conversation backed by the NPC talk API. Merchants expose inline
 * shop controls, innkeepers expose inline rest controls, and only quest_giver
 * NPCs can accept dungeon-clear quests.
 */
import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import {
  INTRO_SCRIPT,
  FIRST_QUEST_ID,
  MERCHANT_DIALOGUE,
  INNKEEPER_DIALOGUE,
  type IntroChoice,
} from "../content/introScript";
import { ListMenu, ErrorState } from "../ui/shell";
import type { ListMenuItem } from "../ui/shell";
import type { NPCAnchorView } from "./NPCBehavior";

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

interface ShopItem {
  item_key: string;
  name: string;
  kind: string;
  price: number;
  qty: number;
  effect: Record<string, unknown>;
}

interface ShopState {
  npc_id: string;
  items: ShopItem[];
}

interface BuyResponse {
  item_key: string;
  qty: number;
  coins: number;
  owned_qty: number;
}

interface RestResponse {
  message: string;
  day: number;
  healed: unknown[];
}

interface NPCDialogueProps {
  runId: string | null;
  npc: NPCAnchorView | null;
  onClose: () => void;
  onOnboarded?: () => void;
  onQuestSettled?: () => void;
}

export function NPCDialogue({
  runId,
  npc,
  onClose,
  onOnboarded,
  onQuestSettled,
}: NPCDialogueProps) {
  const isIntro = npc?.npc_id === INTRO_SCRIPT.npcId;

  if (!npc) return null;
  return isIntro ? (
    <IntroDialogue runId={runId} onClose={onClose} onOnboarded={onOnboarded} />
  ) : (
    <GenericDialogue
      runId={runId}
      npc={npc}
      onClose={onClose}
      onQuestSettled={onQuestSettled}
    />
  );
}

function DialogueDrawer({
  name,
  archetype,
  onClose,
  children,
}: {
  name: string;
  archetype?: string;
  onClose: () => void;
  children: ReactNode;
}) {
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
            Close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function IntroDialogue({
  runId,
  onClose,
  onOnboarded,
}: {
  runId: string | null;
  onClose: () => void;
  onOnboarded?: () => void;
}) {
  const [lineIdx, setLineIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const lines = INTRO_SCRIPT.lines;
  const atChoices = lineIdx >= lines.length;
  const current = lines[Math.min(lineIdx, lines.length - 1)];

  const advance = useCallback(() => {
    setLineIdx((i) => Math.min(i + 1, lines.length));
  }, [lines.length]);

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
        setLineIdx(1);
        return;
      }
      if (choice.effect === "decline") {
        onClose();
        return;
      }
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
    <DialogueDrawer
      name={INTRO_SCRIPT.npcName}
      archetype={INTRO_SCRIPT.npcArchetype}
      onClose={onClose}
    >
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
            click / Enter to continue ({lineIdx + 1}/{lines.length})
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
              Summoning your first Speaker...
            </p>
          ) : null}
        </div>
      )}
    </DialogueDrawer>
  );
}

function GenericDialogue({
  runId,
  npc,
  onClose,
  onQuestSettled,
}: {
  runId: string | null;
  npc: NPCAnchorView;
  onClose: () => void;
  onQuestSettled?: () => void;
}) {
  const [messages, setMessages] = useState<TalkTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [quest, setQuest] = useState<QuestResponse["quest"]>(null);
  const [questLoading, setQuestLoading] = useState(false);
  const [questError, setQuestError] = useState<string | null>(null);
  const [shop, setShop] = useState<ShopState | null>(null);
  const [shopLoading, setShopLoading] = useState(false);
  const [shopError, setShopError] = useState<string | null>(null);
  const [shopMessage, setShopMessage] = useState<string | null>(null);
  const [buyingItem, setBuyingItem] = useState<string | null>(null);
  const [restLoading, setRestLoading] = useState(false);
  const [restMessage, setRestMessage] = useState<string | null>(null);
  const [restError, setRestError] = useState<string | null>(null);

  const canOfferQuest = npc.archetype === "quest_giver";
  const canShop = npc.archetype === "merchant";
  const canRest = npc.archetype === "innkeeper";

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
    setShop(null);
    setShopError(null);
    setShopMessage(null);
    setShopLoading(false);
    setBuyingItem(null);
    setRestMessage(null);
    setRestError(null);
    setRestLoading(false);

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
        if (canShop) setMessages([{ role: "npc", text: MERCHANT_DIALOGUE.greeting }]);
        else if (canRest) setMessages([{ role: "npc", text: INNKEEPER_DIALOGUE.greeting }]);
        else setError("The path is silent.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [runId, npc.npc_id, canShop, canRest]);

  useEffect(() => {
    if (!runId) return;
    api.post(`/api/runs/${runId}/npc/${npc.npc_id}/debated`).catch(() => {});
  }, [runId, npc.npc_id]);

  const sendMessage = (event: FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (!runId || !text || loading) return;
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
    if (!runId || questLoading) return;
    setQuestLoading(true);
    setQuestError(null);
    api
      .post<QuestResponse>(`/api/runs/${runId}/quest/accept`, { npc_id: npc.npc_id })
      .then((res) => {
        setQuest(res.quest);
        if (!res.quest) setQuestError("No work nearby.");
      })
      .catch(() => setQuestError("The notice board is blank."))
      .finally(() => {
        setQuestLoading(false);
        onQuestSettled?.();
      });
  };

  const openShop = () => {
    if (shopLoading) return;
    setShopLoading(true);
    setShopError(null);
    setShopMessage(null);
    api
      .get<ShopState>(`/api/shop/${npc.npc_id}`)
      .then((res) => setShop(res))
      .catch(() => setShopError("The merchant turns you away."))
      .finally(() => setShopLoading(false));
  };

  const buyItem = (item: ShopItem) => {
    if (!runId || buyingItem) return;
    setBuyingItem(item.item_key);
    setShopError(null);
    setShopMessage(null);
    api
      .post<BuyResponse>(
        `/api/shop/${npc.npc_id}/buy?run_id=${encodeURIComponent(runId)}`,
        { item_key: item.item_key, qty: 1 }
      )
      .then((res) => {
        setShop((current) =>
          current
            ? {
                ...current,
                items: current.items.map((cur) =>
                  cur.item_key === res.item_key
                    ? { ...cur, qty: Math.max(0, cur.qty - res.qty) }
                    : cur
                ),
              }
            : current
        );
        setShopMessage(`Bought ${item.name}. Coins: ${res.coins}. Owned: ${res.owned_qty}.`);
      })
      .catch(() => setShopError("The merchant turns you away."))
      .finally(() => setBuyingItem(null));
  };

  const makeCamp = () => {
    if (!runId || restLoading) return;
    setRestLoading(true);
    setRestError(null);
    setRestMessage(null);
    api
      .post<RestResponse>(`/api/runs/${runId}/rest`)
      .then((res) => setRestMessage(`${res.message} Day ${res.day}.`))
      .catch(() => setRestError("You can't make camp here."))
      .finally(() => setRestLoading(false));
  };

  const shopItems: ListMenuItem<ShopItem>[] =
    shop?.items.map((item) => ({
      id: item.item_key,
      label: item.name,
      hint: `${item.kind.replaceAll("_", " ")} - stock ${item.qty}`,
      trailing: `${item.price}c`,
      disabled: item.qty <= 0 || buyingItem === item.item_key,
      value: item,
    })) ?? [];

  return (
    <DialogueDrawer name={npc.name || npc.npc_id} archetype={npc.archetype} onClose={onClose}>
      {error && messages.length === 0 ? (
        <ErrorState message="The path is silent." detail={error} />
      ) : (
        <div className="space-y-2">
          {messages.length === 0 && loading ? (
            <p className="font-body text-sm min-h-10 leading-relaxed" style={{ color: "var(--ink)" }}>
              ...
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
              placeholder="Say something..."
            />
            <button
              className="pixel-btn pixel-btn--accent text-[9px] py-1"
              type="submit"
              disabled={loading || !draft.trim()}
            >
              {loading ? "..." : "Say"}
            </button>
          </form>

          {canShop ? (
            <div
              className="mt-3 border-t pt-3"
              style={{ borderColor: "rgba(232,230,216,0.16)" }}
            >
              <div className="flex flex-wrap items-center gap-2">
                <button
                  className="pixel-btn pixel-btn--accent text-[9px] py-1"
                  onClick={openShop}
                  disabled={shopLoading}
                >
                  {shopLoading ? "Opening..." : shop ? "Refresh shop" : "Shop"}
                </button>
                {shopMessage ? (
                  <span className="font-body text-xs" style={{ color: "var(--muted)" }}>
                    {shopMessage}
                  </span>
                ) : null}
                {shopError ? (
                  <span className="font-body text-xs" style={{ color: "var(--danger)" }}>
                    {shopError}
                  </span>
                ) : null}
              </div>
              {shop ? (
                <ListMenu
                  items={shopItems}
                  onSelect={(item) => item.value && buyItem(item.value)}
                  autoFocus={false}
                  ariaLabel={`${npc.name || npc.npc_id} shop`}
                  className="mt-2 max-h-44 overflow-auto"
                />
              ) : null}
            </div>
          ) : null}

          {canRest ? (
            <div
              className="mt-3 border-t pt-3"
              style={{ borderColor: "rgba(232,230,216,0.16)" }}
            >
              <div className="flex flex-wrap items-center gap-2">
                <button
                  className="pixel-btn pixel-btn--accent text-[9px] py-1"
                  onClick={makeCamp}
                  disabled={restLoading}
                >
                  {restLoading ? "Resting..." : "Make camp"}
                </button>
                {restMessage ? (
                  <span className="font-body text-xs" style={{ color: "var(--muted)" }}>
                    {restMessage}
                  </span>
                ) : null}
                {restError ? (
                  <span className="font-body text-xs" style={{ color: "var(--danger)" }}>
                    {restError}
                  </span>
                ) : null}
              </div>
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
