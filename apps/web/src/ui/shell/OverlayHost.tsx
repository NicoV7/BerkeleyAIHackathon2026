/**
 * OverlayHost — renders whichever Adventure-menu overlay surface is open
 * (store `overlay`) inside a <ModalScrim> + <MenuPanel>. It owns the frame so
 * surface owners only fill the BODY of their surface.
 *
 *   - "inventory" → InventoryScreen (WS-2, #15)
 *   - "quests"    → QuestLogScreen (WS-2, #7)
 *   - "map"       → placeholder (the OVERWORLD owner ships the full map view)
 *
 * Diegetic surfaces (Camp, Shop) are NOT in the overlay union — they own their
 * own ModalScrim and are rendered by App from store `atCamp` / `shopNpcId`.
 */
import { useGame } from "../../state/store";
import { EmptyState } from "./EmptyState";
import { MenuPanel } from "./MenuPanel";
import { ModalScrim } from "./ModalScrim";
import InventoryScreen from "../InventoryScreen";
import QuestLogScreen from "../QuestLogScreen";

const TITLES: Record<string, string> = {
  inventory: "Inventory",
  quests: "Quest Log",
  map: "World Map",
};

export function OverlayHost() {
  const overlay = useGame((s) => s.overlay);
  const closeOverlay = useGame((s) => s.closeOverlay);

  if (!overlay) return null;

  return (
    <ModalScrim onClose={closeOverlay}>
      <MenuPanel title={TITLES[overlay] ?? overlay} onClose={closeOverlay}>
        {renderSurface(overlay)}
      </MenuPanel>
    </ModalScrim>
  );
}

function renderSurface(overlay: NonNullable<ReturnType<typeof useGame.getState>["overlay"]>) {
  switch (overlay) {
    case "inventory":
      return <InventoryScreen />;
    case "quests":
      return <QuestLogScreen />;
    case "map":
      // The full world-map view is the OVERWORLD owner's surface (the HUD
      // minimap in OverworldHud stays separate). Placeholder until then.
      return (
        <EmptyState
          icon="🗺"
          title="Nothing charted yet"
          message="Explore to reveal the map."
        />
      );
    default:
      return null;
  }
}

export default OverlayHost;
