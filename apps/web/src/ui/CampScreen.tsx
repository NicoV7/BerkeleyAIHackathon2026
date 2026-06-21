/**
 * CampScreen (WS-3, issue #11) — the diegetic Dragon-Quest-style campsite.
 * Entered by talking to an innkeeper NPC / walking into a camp POI (store
 * `atCamp`), NOT from the Adventure menu. Presents as an overlay modal.
 *
 * Three actions, gated by the run's camp_token count:
 *   - Rest      → consumes a camp_token (POST inventory/use camp_token) then
 *                 fully heals the party (POST /api/runs/{id}/rest).
 *   - Train     → pick a party agent + a training item → POST /api/training/quick
 *                 {monster_id, item_key} (permanent stat gain).
 *   - Talk      → canned, party-driven lines from your Speakers.
 *
 * Camp uses are limited by camp_token quantity: with zero tokens Rest is
 * disabled and the empty/disabled state explains why. Self-contained: owns its
 * own ModalScrim + MenuPanel.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import {
  ModalScrim,
  MenuPanel,
  ListMenu,
  LoadingState,
  ErrorState,
} from "./shell";
import type { ListMenuItem } from "./shell";
import { itemGlyph, itemBlurb, isTrainingItem } from "../content/itemMeta";

const CAMP_TOKEN_KEY = "camp_token";

interface InventoryItem {
  item_key: string;
  name: string;
  kind: string;
  qty: number;
  effect: Record<string, unknown>;
}

interface MonsterSummary {
  id: string;
  name: string;
  type: string;
  level: number;
}

interface RestResult {
  message: string;
  day: number;
}

interface UseItemResult {
  remaining_qty: number;
  message: string;
}

interface QuickTrainResult {
  applied: boolean;
  stats: { atk: number; def: number; mp: number; max_mp: number };
  remaining_qty: number;
  message: string;
}

type View = "menu" | "train" | "talk";

// Canned camp banter; the actual line is keyed to the party member so "Talk"
// feels party-driven without needing an LLM round-trip at the fireside.
const CAMP_LINES = [
  "The fire's good. Sharpen your point before the next debate.",
  "I rehearsed three rebuttals while you slept. Want to hear them?",
  "Rest the body, but never the argument.",
  "I've been thinking about that last exchange. We can do better.",
  "A clear head wins more debates than a loud one.",
];

export default function CampScreen() {
  const runId = useGame((s) => s.runId);
  const closeCamp = useGame((s) => s.closeCamp);

  const [inventory, setInventory] = useState<InventoryItem[] | null>(null);
  const [party, setParty] = useState<MonsterSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [view, setView] = useState<View>("menu");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  // Two-step Train flow: pick an agent, then pick a training item.
  const [trainMonster, setTrainMonster] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    try {
      const [inv, p] = await Promise.all([
        api.get<InventoryItem[]>(`/api/runs/${runId}/inventory`),
        api.get<MonsterSummary[]>(`/api/runs/${runId}/party`),
      ]);
      setInventory(inv);
      setParty(p);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to make camp.");
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  const campTokens = useMemo(
    () => inventory?.find((i) => i.item_key === CAMP_TOKEN_KEY)?.qty ?? 0,
    [inventory]
  );
  const trainingItems = useMemo(
    () => (inventory ?? []).filter((i) => isTrainingItem(i.kind) && i.qty > 0),
    [inventory]
  );

  const rest = useCallback(async () => {
    if (!runId || busy || campTokens <= 0) return;
    setBusy("rest");
    setFlash(null);
    try {
      // Consume a camp token first (gates camp uses), then heal the party.
      const used = await api.post<UseItemResult>(`/api/runs/${runId}/inventory/use`, {
        item_key: CAMP_TOKEN_KEY,
      });
      const result = await api.post<RestResult>(`/api/runs/${runId}/rest`);
      setInventory((prev) =>
        (prev ?? [])
          .map((it) =>
            it.item_key === CAMP_TOKEN_KEY ? { ...it, qty: used.remaining_qty } : it
          )
          .filter((it) => it.qty > 0)
      );
      setFlash(`${result.message} (Day ${result.day})`);
    } catch (e) {
      setFlash(e instanceof Error ? `Couldn't rest: ${e.message}` : "Couldn't rest.");
    } finally {
      setBusy(null);
    }
  }, [runId, busy, campTokens]);

  const trainWith = useCallback(
    async (item: InventoryItem) => {
      if (!runId || !trainMonster || busy) return;
      setBusy(item.item_key);
      setFlash(null);
      try {
        const res = await api.post<QuickTrainResult>(`/api/training/quick`, {
          monster_id: trainMonster,
          item_key: item.item_key,
        });
        setInventory((prev) =>
          (prev ?? [])
            .map((it) =>
              it.item_key === item.item_key ? { ...it, qty: res.remaining_qty } : it
            )
            .filter((it) => it.qty > 0)
        );
        setFlash(res.message);
        setView("menu");
        setTrainMonster(null);
      } catch (e) {
        setFlash(e instanceof Error ? `Training failed: ${e.message}` : "Training failed.");
      } finally {
        setBusy(null);
      }
    },
    [runId, trainMonster, busy]
  );

  // ---- Body by view ----
  let body: React.ReactNode;
  let onBack: (() => void) | undefined;

  if (loading && inventory === null) {
    body = <LoadingState label="Setting up camp" />;
  } else if (error) {
    body = <ErrorState message="You can't make camp here." detail={error} />;
  } else if (view === "train") {
    onBack = () => {
      setView("menu");
      setTrainMonster(null);
    };
    if (!trainMonster) {
      const rows: ListMenuItem<MonsterSummary>[] = (party ?? []).map((m) => ({
        id: m.id,
        label: m.name,
        trailing: `L${m.level}`,
        hint: m.type,
        disabled: busy !== null,
        value: m,
      }));
      body = (
        <div className="space-y-2">
          <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
            Which Speaker will you train?
          </div>
          {rows.length === 0 ? (
            <p className="font-body text-xs" style={{ color: "var(--muted)" }}>
              No party members to train yet.
            </p>
          ) : (
            <ListMenu
              items={rows}
              ariaLabel="Choose agent to train"
              onSelect={(row) => row.value && setTrainMonster(row.value.id)}
            />
          )}
        </div>
      );
    } else {
      const rows: ListMenuItem<InventoryItem>[] = trainingItems.map((it) => ({
        id: it.item_key,
        label: `${itemGlyph(it.kind)} ${it.name}`,
        trailing: busy === it.item_key ? "…" : `×${it.qty}`,
        hint: itemBlurb(it.kind, it.effect),
        disabled: busy !== null,
        value: it,
      }));
      const who = party?.find((m) => m.id === trainMonster)?.name ?? "agent";
      body = (
        <div className="space-y-2">
          <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
            Spend a training item on {who}:
          </div>
          {rows.length === 0 ? (
            <p className="font-body text-xs" style={{ color: "var(--muted)" }}>
              No training items. Buy a Whetstone, Aegis Tome, or Focus Charm at the shop.
            </p>
          ) : (
            <ListMenu
              items={rows}
              ariaLabel="Choose training item"
              onSelect={(row) => row.value && trainWith(row.value)}
            />
          )}
        </div>
      );
    }
  } else if (view === "talk") {
    onBack = () => setView("menu");
    const rows: ListMenuItem<string>[] = (party ?? []).map((m, i) => ({
      id: m.id,
      label: m.name,
      hint: CAMP_LINES[i % CAMP_LINES.length],
      value: m.id,
    }));
    body = (
      <div className="space-y-2">
        <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
          Your Speakers gather around the fire.
        </div>
        {rows.length === 0 ? (
          <p className="font-body text-xs" style={{ color: "var(--muted)" }}>
            No Speakers to talk to yet — summon one first.
          </p>
        ) : (
          <ListMenu items={rows} ariaLabel="Talk to party" autoFocus={false} onSelect={() => {}} />
        )}
      </div>
    );
  } else {
    // Main camp menu.
    const rows: ListMenuItem<View | "rest">[] = [
      {
        id: "rest",
        label: "🔥 Rest",
        trailing: `🏕 ×${campTokens}`,
        hint:
          campTokens > 0
            ? "Spend a camp token to fully heal your party and advance the day."
            : "No camp tokens — buy one at the shop to rest.",
        disabled: campTokens <= 0 || busy !== null,
        value: "rest",
      },
      {
        id: "train",
        label: "🧬 Train an agent",
        trailing: `${trainingItems.length} item${trainingItems.length === 1 ? "" : "s"}`,
        hint: "Spend a training item to permanently raise a Speaker's stat.",
        disabled: (party?.length ?? 0) === 0 || busy !== null,
        value: "train",
      },
      {
        id: "talk",
        label: "💬 Talk to your party",
        trailing: `${party?.length ?? 0}`,
        hint: "Trade a word with your Speakers by the fire.",
        disabled: (party?.length ?? 0) === 0,
        value: "talk",
      },
    ];
    body = (
      <div className="space-y-2">
        <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
          {itemGlyph(CAMP_TOKEN_KEY)} Camp tokens: {campTokens} — each is one rest.
        </div>
        <ListMenu
          items={rows}
          ariaLabel="Camp actions"
          onSelect={(row) => {
            if (row.value === "rest") void rest();
            else setView(row.value as View);
          }}
        />
        {flash ? (
          <p className="font-body text-xs" style={{ color: "var(--win)" }}>
            {flash}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <ModalScrim onClose={closeCamp}>
      <MenuPanel
        title="Camp"
        subtitle={view === "menu" ? "rest · train · talk" : undefined}
        onBack={onBack}
        onClose={closeCamp}
      >
        {body}
      </MenuPanel>
    </ModalScrim>
  );
}
