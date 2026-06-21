/**
 * ErrorState — shared failure placeholder for any surface whose load/action
 * threw. Pixel-RPG flavored: rose glyph + the error line + an optional Retry.
 *
 * Convention: keep `message` short + in-world ("The path is silent."). Put the
 * raw/technical detail (if any) in `detail`, shown muted + small.
 */
import type { ReactNode } from "react";

export interface ErrorStateProps {
  /** Short, in-world failure line. Defaults to "Something went wrong." */
  message?: ReactNode;
  /** Optional technical detail, shown small + muted under the message. */
  detail?: ReactNode;
  /** Optional retry handler. Renders a "Retry" pixel button when set. */
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}

export function ErrorState({
  message = "Something went wrong.",
  detail,
  onRetry,
  retryLabel = "Retry",
  className = "",
}: ErrorStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-3 px-6 py-10 text-center ${className}`}
      role="alert"
    >
      <div className="text-2xl" aria-hidden style={{ color: "var(--danger)" }}>
        ⚠
      </div>
      <p className="font-hud text-[11px]" style={{ color: "var(--danger)" }}>
        {message}
      </p>
      {detail ? (
        <p
          className="font-body text-[10px] max-w-sm leading-relaxed break-words"
          style={{ color: "var(--muted)" }}
        >
          {detail}
        </p>
      ) : null}
      {onRetry ? (
        <button className="pixel-btn text-[10px] mt-1" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

export default ErrorState;
