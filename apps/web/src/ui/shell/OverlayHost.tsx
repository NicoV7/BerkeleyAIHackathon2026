/**
 * OverlayHost — renders whichever Adventure-menu overlay surface is open
 * (store `overlay`) inside a <ModalScrim> + <MenuPanel>. It owns the frame so
 * Wave-2 owners only fill the BODY of their surface.
 *
 * ── WAVE-2 PLACEHOLDERS ──────────────────────────────────────────────────────
 * Each case below is a clearly-marked stub. The owning workstream should REPLACE
 * the placeholder body with their real screen, keeping the <MenuPanel> wrapper
 * (or moving it inside their component) so back/close/Esc grammar stays uniform.
 *   - "inventory" → WS-2 inventory screen
 *   - "quests"    → WS-2 quest log
 *   - "map"       → WS-2 / world-map full view
 * Use the shared primitives for loading/empty/error states (see ./index.ts and
 * UI_CONTRACT.md §State matrix).
 * ─────────────────────────────────────────────────────────────────────────────
 */
import { useGame } from "../../state/store";
import { EmptyState } from "./EmptyState";
import { MenuPanel } from "./MenuPanel";
import { ModalScrim } from "./ModalScrim";

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
      // TODO(WS-2): replace with the real Inventory screen.
      return (
        <EmptyState
          icon="🎒"
          title="Inventory coming soon"
          message="WS-2 will fill this surface. Wire item fetch → LoadingState while pending, EmptyState when empty, ErrorState on failure."
        />
      );
    case "quests":
      // TODO(WS-2): replace with the real Quest Log.
      return (
        <EmptyState
          icon="📜"
          title="No quests yet"
          message="WS-2 will fill this surface. The first quest is granted by the intro NPC (see content/introScript.ts, FIRST_QUEST_ID)."
        />
      );
    case "map":
      // TODO(WS-2): replace with the full world map view.
      return (
        <EmptyState
          icon="🗺"
          title="Map coming soon"
          message="WS-2 will fill this surface. The HUD minimap (OverworldHud) stays separate; this is the full, zoomable map."
        />
      );
    default:
      return null;
  }
}

export default OverlayHost;
