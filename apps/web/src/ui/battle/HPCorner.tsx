import { forwardRef } from "react";
import type { CombatantState } from "../../ws/useEncounterStream";

function HpBar({ hp, max_hp, flipped }: { hp: number; max_hp: number; flipped: boolean }) {
  const segs = 12;
  const pct = max_hp > 0 ? Math.max(0, Math.min(1, hp / max_hp)) : 0;
  const filled = Math.round(pct * segs);
  const color = pct > 0.6 ? "var(--win)" : pct > 0.3 ? "var(--warn)" : "var(--danger)";
  return (
    <div className={`flex gap-[2px] h-2.5 ${flipped ? "flex-row-reverse" : ""}`}>
      {Array.from({ length: segs }).map((_, i) => (
        <div
          key={i}
          className="flex-1 transition-colors duration-300"
          style={{ background: i < filled ? color : "rgba(232,230,216,0.10)" }}
        />
      ))}
    </div>
  );
}

export const HPCorner = forwardRef<
  HTMLDivElement,
  {
    side: "player" | "enemy";
    combatant: CombatantState | null;
    floatDmg: number | null;
    floatKey?: number | string;
  }
>(function HPCorner({ side, combatant, floatDmg, floatKey }, ref) {
  const isPlayer = side === "player";
  const color = isPlayer ? "var(--party)" : "var(--enemy)";
  const pct = combatant && combatant.max_hp > 0 ? combatant.hp / combatant.max_hp : 1;
  const hpColor = pct > 0.6 ? "var(--win)" : pct > 0.3 ? "var(--warn)" : "var(--danger)";

  return (
    <div
      ref={ref}
      className="pixel-panel relative shrink-0"
      style={{
        width: 188,
        borderColor: color,
        background: "rgba(14,16,24,0.90)",
        padding: "6px 10px",
      }}
    >
      {/* Damage float */}
      {floatDmg != null && floatDmg > 0 && (
        <div
          key={`dmg-${floatKey ?? 0}`}
          className="dmg-float font-display absolute pointer-events-none"
          style={{
            color: "var(--danger)",
            fontSize: 18,
            top: 0,
            [isPlayer ? "right" : "left"]: 8,
            zIndex: 20,
          }}
        >
          -{floatDmg}
        </div>
      )}

      {/* Layout: player = icon → content; enemy = content → icon */}
      <div className={`flex items-center gap-2 ${isPlayer ? "" : "flex-row-reverse"}`}>
        {/* Portrait glyph */}
        <div
          className="shrink-0 font-hud flex items-center justify-center text-base"
          style={{
            width: 28,
            height: 28,
            border: `2px solid ${color}`,
            background: "rgba(255,255,255,0.04)",
            color,
          }}
        >
          {isPlayer ? "★" : "✦"}
        </div>

        {/* Name + HP text */}
        <div className={`flex-1 min-w-0 ${isPlayer ? "" : "text-right"}`}>
          <div
            className="font-hud text-[10px] truncate"
            style={{ color }}
          >
            {combatant?.name ?? "—"}
          </div>
          <div className="font-body text-[9px]" style={{ color: "var(--muted)" }}>
            {combatant ? (
              <span style={{ color: hpColor }}>{combatant.hp}</span>
            ) : null}
            {combatant ? `/${combatant.max_hp} HP` : ""}
          </div>
        </div>
      </div>

      {/* HP bar */}
      <div className="mt-1.5">
        {combatant ? (
          <HpBar hp={combatant.hp} max_hp={combatant.max_hp} flipped={!isPlayer} />
        ) : (
          <div className="h-2.5" style={{ background: "rgba(232,230,216,0.08)" }} />
        )}
      </div>
    </div>
  );
});
