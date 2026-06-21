/**
 * EmptyState — shared "nothing here yet" placeholder for any surface whose
 * collection is empty (empty inventory, no quests, an unstocked shop, …).
 *
 * Pixel-RPG flavored: a big muted glyph, a title, a body line, and an optional
 * call-to-action button. Use this instead of hand-rolling per-surface empties so
 * every screen reads the same.
 *
 * Owners: WS-2 (inventory/quests), WS-3 (dialogue), camp/shop authors.
 */
import type { ReactNode } from "react";

export interface EmptyStateProps {
  /** Big decorative glyph/emoji at the top (e.g. "🎒", "📜"). Optional. */
  icon?: ReactNode;
  /** Short headline, HUD font. e.g. "Your pack is empty". */
  title: string;
  /** One supporting sentence. Optional. */
  message?: ReactNode;
  /** Optional CTA. Renders a pixel button when both label + onAction set. */
  actionLabel?: string;
  onAction?: () => void;
  className?: string;
}

export function EmptyState({
  icon,
  title,
  message,
  actionLabel,
  onAction,
  className = "",
}: EmptyStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-3 px-6 py-10 text-center ${className}`}
      role="status"
    >
      {icon ? (
        <div className="text-3xl opacity-70" aria-hidden>
          {icon}
        </div>
      ) : null}
      <p className="font-hud text-[12px]" style={{ color: "var(--ink)" }}>
        {title}
      </p>
      {message ? (
        <p className="font-body text-xs max-w-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          {message}
        </p>
      ) : null}
      {actionLabel && onAction ? (
        <button className="pixel-btn pixel-btn--accent text-[10px] mt-1" onClick={onAction}>
          {actionLabel}
        </button>
      ) : null}
    </div>
  );
}

export default EmptyState;
