/**
 * LoadingState — shared "fetching…" placeholder for any surface awaiting data
 * (party fetch, shop stock, quest log). Pixel-RPG flavored animated ellipsis.
 *
 * Keep this lightweight: it is rendered INSIDE a MenuPanel body, so it should
 * not draw its own panel chrome.
 */
export interface LoadingStateProps {
  /** Defaults to "Loading…". HUD font. */
  label?: string;
  className?: string;
}

export function LoadingState({ label = "Loading", className = "" }: LoadingStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-2 px-6 py-10 text-center ${className}`}
      role="status"
      aria-live="polite"
    >
      <span
        className="font-hud text-[11px] pixel-loading-dots"
        style={{ color: "var(--muted)" }}
      >
        {label}
      </span>
    </div>
  );
}

export default LoadingState;
