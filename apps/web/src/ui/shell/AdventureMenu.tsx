/**
 * AdventureMenu — the persistent HUD entry point into the non-diegetic menu
 * surfaces (Inventory / Quests / Map). It lives in the global HUD bar and opens
 * an overlay via the store (`openOverlay`) WITHOUT changing `screen`.
 *
 * Camp and Shop are intentionally NOT here — they are entered diegetically via
 * world POIs (see UI_CONTRACT.md §Nav model). This is just the menu trigger;
 * the surfaces themselves render in <OverlayHost>.
 */
import { useGame } from "../../state/store";
import type { Overlay } from "../../state/store";

const ENTRIES: { key: NonNullable<Overlay>; label: string; glyph: string }[] = [
  { key: "inventory", label: "Items", glyph: "🎒" },
  { key: "quests", label: "Quests", glyph: "📜" },
  { key: "map", label: "Map", glyph: "🗺" },
];

export function AdventureMenu({ className = "" }: { className?: string }) {
  const overlay = useGame((s) => s.overlay);
  const openOverlay = useGame((s) => s.openOverlay);
  const closeOverlay = useGame((s) => s.closeOverlay);

  return (
    <div className={`flex items-center gap-1 ${className}`}>
      {ENTRIES.map((e) => {
        const isOpen = overlay === e.key;
        return (
          <button
            key={e.key}
            className={`pixel-btn text-[9px] py-0.5 ${isOpen ? "pixel-btn--accent" : ""}`}
            aria-pressed={isOpen}
            title={e.label}
            onClick={() => (isOpen ? closeOverlay() : openOverlay(e.key))}
          >
            <span aria-hidden className="mr-1">
              {e.glyph}
            </span>
            {e.label}
          </button>
        );
      })}
    </div>
  );
}

export default AdventureMenu;
