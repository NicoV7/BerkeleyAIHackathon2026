/**
 * MenuPanel — the standard titled container for every overlay surface
 * (Inventory, Quests, Camp, Shop, Dialogue). It owns the shared chrome so the
 * Wave-2 authors only write the BODY of their screen.
 *
 * Responsibilities:
 *  - Pixel panel chrome + title bar (HUD font, gold accent).
 *  - Optional Back affordance (left) and Close affordance (right) with the
 *    UNIVERSAL back/close grammar (see UI_CONTRACT.md §Back/Close).
 *  - Esc-to-close (when `onClose` set + `closeOnEsc`, default true).
 *  - Optional `footer` slot for surface-specific actions (Buy, Equip, …).
 *
 * Layout note: this is the surface's frame, NOT its backdrop. For a MODAL
 * presentation, wrap it in <ModalScrim>. For a full-screen surface, render it
 * inside the <main> region. See UI_CONTRACT.md §Presentation.
 */
import { useEffect } from "react";
import type { ReactNode } from "react";

export interface MenuPanelProps {
  title: ReactNode;
  /** Optional small sub-label shown next to the title (muted HUD). */
  subtitle?: ReactNode;
  /** Show a "← Back" affordance on the left of the title bar. */
  onBack?: () => void;
  backLabel?: string;
  /** Show a "✕" close affordance on the right of the title bar. */
  onClose?: () => void;
  /** Close when the user presses Escape. Default true (only if onClose set). */
  closeOnEsc?: boolean;
  /** Optional sticky footer region for primary actions. */
  footer?: ReactNode;
  /** Constrain the panel width (Tailwind max-w-*). Default "max-w-2xl". */
  maxWidthClassName?: string;
  className?: string;
  children: ReactNode;
}

export function MenuPanel({
  title,
  subtitle,
  onBack,
  backLabel = "Back",
  onClose,
  closeOnEsc = true,
  footer,
  maxWidthClassName = "max-w-2xl",
  className = "",
  children,
}: MenuPanelProps) {
  useEffect(() => {
    if (!onClose || !closeOnEsc) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, closeOnEsc]);

  return (
    <section
      className={`pixel-panel w-full ${maxWidthClassName} mx-auto flex flex-col ${className}`}
      role="dialog"
      aria-label={typeof title === "string" ? title : undefined}
    >
      {/* Title bar */}
      <header
        className="flex items-center gap-2 px-3 py-2"
        style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}
      >
        {onBack ? (
          <button className="pixel-btn text-[9px] py-0.5" onClick={onBack}>
            ← {backLabel}
          </button>
        ) : null}
        <h2 className="font-hud text-[12px] flex items-center gap-2" style={{ color: "var(--accent)" }}>
          {title}
          {subtitle ? (
            <span className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
              {subtitle}
            </span>
          ) : null}
        </h2>
        {onClose ? (
          <button
            className="pixel-btn text-[10px] py-0.5 ml-auto"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </button>
        ) : null}
      </header>

      {/* Body */}
      <div className="flex-1 overflow-auto p-3">{children}</div>

      {/* Optional footer */}
      {footer ? (
        <footer
          className="flex items-center justify-end gap-2 px-3 py-2"
          style={{ borderTop: "2px solid rgba(232,230,216,0.12)" }}
        >
          {footer}
        </footer>
      ) : null}
    </section>
  );
}

export default MenuPanel;
