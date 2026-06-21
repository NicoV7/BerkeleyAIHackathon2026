import type { Utterance } from "../../ws/useEncounterStream";
import { PlayerPixelSprite } from "./PlayerPixelSprite";
import { SpeechBubble } from "./SpeechBubble";

const SPRITE_SRC = "/sprites/roguelikeChar_transparent.png";
const SPRITE_COLS = 54;
const SPRITE_STRIDE = 17; // 16px frame + 1px spacing
const SPRITE_SCALE = 3.5;
const SPRITE_FOOTPRINT = Math.round(16 * SPRITE_SCALE); // ≈ 56px

/**
 * A single 16×16 frame from the roguelike character sheet, scaled up with
 * crisp pixel rendering. The outer wrapper reserves the scaled footprint so
 * surrounding layout stays correct; the inner div is the actual sprite frame
 * transformed from its top-left origin.
 */
function CharacterSprite({ frame, flip }: { frame: number; flip?: boolean }) {
  const col = frame % SPRITE_COLS;
  const row = Math.floor(frame / SPRITE_COLS);
  const x = -(col * SPRITE_STRIDE);
  const y = -(row * SPRITE_STRIDE);

  return (
    <div
      style={{
        width: SPRITE_FOOTPRINT,
        height: SPRITE_FOOTPRINT,
        position: "relative",
      }}
    >
      <div
        style={{
          width: 16,
          height: 16,
          backgroundImage: `url(${SPRITE_SRC})`,
          backgroundPosition: `${x}px ${y}px`,
          backgroundRepeat: "no-repeat",
          imageRendering: "pixelated",
          transform: `scale(${SPRITE_SCALE})${flip ? " scaleX(-1)" : ""}`,
          transformOrigin: "top left",
          filter: "drop-shadow(2px 3px 0 rgba(0,0,0,0.6))",
          position: "absolute",
          top: 0,
          left: 0,
        }}
      />
    </div>
  );
}

/**
 * Battle stage: a `position: relative` host for the center column of a battle
 * screen. It renders two real pixel-art sprites facing each other in the
 * middle band, layers the topic strip / outcome banner / speech bubbles, and
 * positions three absolutely-placed slots (HP bars + the floating input box).
 */
export function BattleStage({
  leadPartyName,
  leadEnemyName,
  leadPartyType,
  leadEnemyType,
  playerUtterance,
  enemyUtterance,
  playerIsNewest,
  enemyIsNewest,
  topic,
  isOver,
  phase,
  playerHpSlot,
  enemyHpSlot,
  inputSlot,
}: {
  leadPartyName: string | null;
  leadEnemyName: string | null;
  leadPartyType: string | null;
  leadEnemyType: string | null;
  playerUtterance: Utterance | null;
  enemyUtterance: Utterance | null;
  playerIsNewest: boolean;
  enemyIsNewest: boolean;
  topic: string | null;
  isOver: boolean;
  phase: string;
  playerHpSlot: React.ReactNode;
  enemyHpSlot: React.ReactNode;
  inputSlot: React.ReactNode;
}) {
  const outcomeLabel =
    phase === "won" ? "VICTORY" : phase === "lost" ? "DEFEAT" : null;
  const outcomeColor = phase === "won" ? "var(--win)" : "var(--danger)";

  return (
    <div className="relative flex-1 overflow-hidden select-none">
      {/* Background: subtle radial-gradient wash over the base bg */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse at 50% 60%, rgba(79,122,54,0.12) 0%, transparent 70%)",
        }}
      />
      {/* Pixel grid overlay for "zoomed world" feel */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          backgroundImage:
            "linear-gradient(rgba(232,230,216,0.02) 1px, transparent 1px), linear-gradient(90deg, rgba(232,230,216,0.02) 1px, transparent 1px)",
          backgroundSize: "32px 32px",
        }}
      />

      {/* HP slots — top corners */}
      <div style={{ position: "absolute", top: 8, left: 8, zIndex: 10 }}>
        {playerHpSlot}
      </div>
      <div style={{ position: "absolute", top: 8, right: 8, zIndex: 10 }}>
        {enemyHpSlot}
      </div>

      {/* Topic strip — centered below HP clusters */}
      {topic && !isOver && (
        <div
          className="absolute left-0 right-0 flex justify-center pointer-events-none"
          style={{ top: 96, zIndex: 8 }}
        >
          <span
            className="font-hud text-[11px] px-3 py-1 text-center"
            style={{
              color: "rgba(232,230,216,0.65)",
              background: "rgba(8,9,14,0.55)",
              border: "1px solid rgba(232,230,216,0.15)",
              maxWidth: "70%",
              lineHeight: 1.4,
            }}
          >
            {topic}
          </span>
        </div>
      )}

      {/* Outcome banner */}
      {outcomeLabel && (
        <div
          className="absolute inset-0 flex items-center justify-center pointer-events-none"
          style={{ zIndex: 25 }}
        >
          <span
            className="font-display"
            style={{
              fontSize: 48,
              color: outcomeColor,
              textShadow: "4px 4px 0 #000",
              opacity: 0.9,
            }}
          >
            {outcomeLabel}
          </span>
        </div>
      )}

      {/* PLAYER — left band, sprite faces right (toward enemy) */}
      <div
        className="absolute flex flex-col items-center"
        style={{
          left: "26%",
          top: "36%",
          transform: "translateX(-50%)",
          zIndex: 2,
        }}
      >
        {/* Bubble grows upward, sits above the sprite */}
        <div style={{ position: "relative", zIndex: 15 }}>
          <SpeechBubble
            utterance={playerUtterance}
            isNewest={playerIsNewest}
            side="player"
          />
        </div>

        {/* Name tag floats above the sprite */}
        {leadPartyName && (
          <div
            className="font-hud text-[8px] mb-1 px-1 whitespace-nowrap"
            style={{
              color: "var(--party)",
              textShadow: "1px 1px 0 #000",
              background: "rgba(0,0,0,0.55)",
              border: "1px solid rgba(92,200,255,0.25)",
            }}
          >
            {leadPartyName}
            {leadPartyType && (
              <span className="ml-1 opacity-60">[{leadPartyType}]</span>
            )}
          </div>
        )}

        <PlayerPixelSprite />
      </div>

      {/* ENEMY — right band, sprite faces left (toward player) */}
      <div
        className="absolute flex flex-col items-center"
        style={{
          left: "74%",
          top: "36%",
          transform: "translateX(-50%)",
          zIndex: 2,
        }}
      >
        <div style={{ position: "relative", zIndex: 15 }}>
          <SpeechBubble
            utterance={enemyUtterance}
            isNewest={enemyIsNewest}
            side="enemy"
          />
        </div>

        {/* Name tag floats above the sprite */}
        {leadEnemyName && (
          <div
            className="font-hud text-[8px] mb-1 px-1 whitespace-nowrap"
            style={{
              color: "var(--enemy)",
              textShadow: "1px 1px 0 #000",
              background: "rgba(0,0,0,0.55)",
              border: "1px solid rgba(255,93,108,0.25)",
            }}
          >
            {leadEnemyName}
            {leadEnemyType && (
              <span className="ml-1 opacity-60">[{leadEnemyType}]</span>
            )}
          </div>
        )}

        <CharacterSprite frame={7} flip />
      </div>

      {/* VS divider */}
      <div
        className="absolute font-display"
        style={{
          left: "50%",
          top: "44%",
          transform: "translate(-50%, -50%)",
          fontSize: 18,
          color: "rgba(232,230,216,0.12)",
          textShadow: "1px 1px 0 #000",
          pointerEvents: "none",
          zIndex: 1,
        }}
      >
        ⚔
      </div>

      {/* Input slot — floating lower-center */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          transform: "translateX(-50%)",
          bottom: 16,
          width: "min(92%, 520px)",
          zIndex: 20,
        }}
      >
        {inputSlot}
      </div>
    </div>
  );
}
