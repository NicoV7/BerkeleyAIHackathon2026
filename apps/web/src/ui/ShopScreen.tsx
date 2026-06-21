/**
 * ShopScreen (WS-3, issues #13/#14) — the diegetic merchant shop. Entered by
 * talking to a merchant NPC / walking into a shop POI (store `shopNpcId`), NOT
 * from the Adventure menu. Presents as an overlay modal (UI_CONTRACT.md rec.).
 *
 * Lists GET /api/shop/{npc} and buys via POST /api/shop/{npc}/buy?run_id={id}.
 * The wallet (GET /api/runs/{id}/wallet) is shown in the title bar; items the
 * player can't afford are disabled (dimmed) with the price still visible in
 * `trailing`, per the state matrix. Self-contained: it owns its ModalScrim +
 * MenuPanel so App.tsx can render it unconditionally when a shop is open.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import {
  ModalScrim,
  MenuPanel,
  ListMenu,
  LoadingState,
  EmptyState,
  ErrorState,
} from "./shell";
import type { ListMenuItem } from "./shell";
import { itemGlyph, itemBlurb } from "../content/itemMeta";

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

interface WalletState {
  coins: number;
}

interface BuyItemResult {
  item_key: string;
  spent: number;
  coins: number;
  owned_qty: number;
}

export default function ShopScreen() {
  const runId = useGame((s) => s.runId);
  const npcId = useGame((s) => s.shopNpcId);
  const closeShop = useGame((s) => s.closeShop);

  const [stock, setStock] = useState<ShopItem[] | null>(null);
  const [coins, setCoins] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    if (!runId || !npcId) return;
    setLoading(true);
    setError(null);
    try {
      const [shop, wallet] = await Promise.all([
        api.get<ShopState>(`/api/shop/${npcId}`),
        api.get<WalletState>(`/api/runs/${runId}/wallet`).catch(() => null),
      ]);
      setStock(shop.items ?? []);
      if (wallet) setCoins(wallet.coins);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to open the shop.");
    } finally {
      setLoading(false);
    }
  }, [runId, npcId]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  const buy = useCallback(
    async (item: ShopItem) => {
      if (!runId || !npcId || busyKey) return;
      setBusyKey(item.item_key);
      setFlash(null);
      try {
        const res = await api.post<BuyItemResult>(
          `/api/shop/${npcId}/buy?run_id=${encodeURIComponent(runId)}`,
          { item_key: item.item_key, qty: 1 }
        );
        setCoins(res.coins);
        setFlash(`Bought ${item.name}. −🪙${res.spent}`);
        // Reflect the decremented stock locally.
        setStock((prev) =>
          (prev ?? []).map((it) =>
            it.item_key === item.item_key ? { ...it, qty: Math.max(0, it.qty - 1) } : it
          )
        );
      } catch (e) {
        setFlash(
          e instanceof Error ? e.message.replace(/^\d+\s/, "Can't buy: ") : "Couldn't buy that."
        );
      } finally {
        setBusyKey(null);
      }
    },
    [runId, npcId, busyKey]
  );

  if (!npcId) return null;

  let body: React.ReactNode;
  if (loading && stock === null) {
    body = <LoadingState label="Browsing the wares" />;
  } else if (error) {
    body = (
      <ErrorState message="The merchant turns you away." detail={error} onRetry={() => void fetchAll()} />
    );
  } else if (!stock || stock.length === 0) {
    body = (
      <EmptyState icon="🪙" title="Out of stock" message="Come back later — the merchant is restocking." />
    );
  } else {
    const rows: ListMenuItem<ShopItem>[] = stock.map((it) => {
      const broke = coins !== null && coins < it.price;
      const sold = it.qty <= 0;
      return {
        id: it.item_key,
        label: `${itemGlyph(it.kind)} ${it.name}`,
        trailing: busyKey === it.item_key ? "…" : sold ? "SOLD OUT" : `🪙 ${it.price}`,
        hint: itemBlurb(it.kind, it.effect),
        disabled: broke || sold || busyKey !== null,
        value: it,
      };
    });
    body = (
      <div className="space-y-2">
        <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
          Select an item to buy it. Greyed items cost more than you carry.
        </div>
        <ListMenu items={rows} ariaLabel="Shop wares" onSelect={(row) => row.value && buy(row.value)} />
        {flash ? (
          <p className="font-body text-xs" style={{ color: "var(--win)" }}>
            {flash}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <ModalScrim onClose={closeShop}>
      <MenuPanel
        title="Shop"
        subtitle={coins !== null ? `🪙 ${coins}` : undefined}
        onClose={closeShop}
      >
        {body}
      </MenuPanel>
    </ModalScrim>
  );
}
