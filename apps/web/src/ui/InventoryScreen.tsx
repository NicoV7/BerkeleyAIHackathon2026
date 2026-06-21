/**
 * InventoryScreen (WS-2, issue #15) — the body of the "inventory" Adventure-menu
 * overlay. Lists the run's owned items via GET /api/runs/{id}/inventory and lets
 * the player USE potions / camp tokens via POST /api/runs/{id}/inventory/use.
 *
 * Rendered inside OverlayHost's <MenuPanel> frame, so this component only paints
 * the BODY (the four state-matrix states from UI_CONTRACT.md §State matrix) plus
 * a wallet line. Training items are shown disabled here (they apply through the
 * Camp "train" flow) so the affordance stays visible without a dead action.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import { ListMenu, LoadingState, EmptyState, ErrorState } from "./shell";
import type { ListMenuItem } from "./shell";
import { itemGlyph, itemBlurb, isUsableInInventory, isTrainingItem } from "../content/itemMeta";

interface InventoryItem {
  item_key: string;
  name: string;
  kind: string;
  qty: number;
  effect: Record<string, unknown>;
  price: number;
}

interface UseItemResult {
  item_key: string;
  applied: Record<string, unknown>;
  remaining_qty: number;
  message: string;
}

interface WalletState {
  coins: number;
}

export default function InventoryScreen() {
  const runId = useGame((s) => s.runId);
  const [items, setItems] = useState<InventoryItem[] | null>(null);
  const [coins, setCoins] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    try {
      const [inv, wallet] = await Promise.all([
        api.get<InventoryItem[]>(`/api/runs/${runId}/inventory`),
        api.get<WalletState>(`/api/runs/${runId}/wallet`).catch(() => null),
      ]);
      setItems(inv);
      if (wallet) setCoins(wallet.coins);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to read inventory.");
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  const useItem = useCallback(
    async (item: InventoryItem) => {
      if (!runId || busyKey) return;
      setBusyKey(item.item_key);
      setFlash(null);
      try {
        const res = await api.post<UseItemResult>(`/api/runs/${runId}/inventory/use`, {
          item_key: item.item_key,
        });
        setFlash(res.message);
        // Reflect the new quantity locally, then refresh the wallet too.
        setItems((prev) =>
          (prev ?? [])
            .map((it) =>
              it.item_key === item.item_key ? { ...it, qty: res.remaining_qty } : it
            )
            .filter((it) => it.qty > 0)
        );
        api
          .get<WalletState>(`/api/runs/${runId}/wallet`)
          .then((w) => setCoins(w.coins))
          .catch(() => {});
      } catch (e) {
        setFlash(e instanceof Error ? `Couldn't use that: ${e.message}` : "Couldn't use that.");
      } finally {
        setBusyKey(null);
      }
    },
    [runId, busyKey]
  );

  if (loading && items === null) return <LoadingState label="Reading your pack" />;
  if (error)
    return (
      <ErrorState message="Couldn't open your pack." detail={error} onRetry={() => void fetchAll()} />
    );
  if (!items || items.length === 0)
    return (
      <EmptyState
        icon="🎒"
        title="Your pack is empty"
        message="Loot and rewards land here. Win debates to fill it."
      />
    );

  const rows: ListMenuItem<InventoryItem>[] = items.map((it) => {
    const usable = isUsableInInventory(it.kind);
    const training = isTrainingItem(it.kind);
    return {
      id: it.item_key,
      label: `${itemGlyph(it.kind)} ${it.name}`,
      trailing: busyKey === it.item_key ? "…" : `×${it.qty}`,
      hint: training ? `${itemBlurb(it.kind)} (use at camp)` : itemBlurb(it.kind),
      // Training items aren't activatable here — they apply via the Camp "train"
      // flow — so we dim them but keep them visible (state-matrix "disabled").
      disabled: !usable || busyKey !== null,
      value: it,
    };
  });

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between font-hud text-[10px]">
        <span style={{ color: "var(--muted)" }}>Select an item to use it.</span>
        {coins !== null ? (
          <span style={{ color: "var(--accent)" }}>🪙 {coins}</span>
        ) : null}
      </div>
      <ListMenu
        items={rows}
        ariaLabel="Inventory"
        onSelect={(row) => row.value && useItem(row.value)}
      />
      {flash ? (
        <p className="font-body text-xs" style={{ color: "var(--win)" }}>
          {flash}
        </p>
      ) : null}
    </div>
  );
}
