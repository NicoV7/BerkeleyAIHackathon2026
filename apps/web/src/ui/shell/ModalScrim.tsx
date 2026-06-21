/**
 * ModalScrim — the dimmed full-viewport backdrop that hosts a MODAL or the
 * Adventure-menu overlay surfaces (Inventory/Quests/Map). It centers its child
 * (typically a <MenuPanel>) and dims everything behind.
 *
 * Click-outside closes (when `onClose` set + `closeOnBackdrop`, default true).
 * Escape handling lives on MenuPanel; the scrim only owns the backdrop.
 *
 * z-order (see UI_CONTRACT.md §Z-order): scrim sits at z-50, above the HUD
 * (z-10..z-40) and the Phaser canvas, below the iris transition (z-9999).
 */
import type { MouseEvent, ReactNode } from "react";

export interface ModalScrimProps {
  onClose?: () => void;
  /** Close when the backdrop (not the child) is clicked. Default true. */
  closeOnBackdrop?: boolean;
  /** Tailwind alignment of the child. Default centered. */
  align?: "center" | "top" | "bottom";
  className?: string;
  children: ReactNode;
}

const ALIGN: Record<NonNullable<ModalScrimProps["align"]>, string> = {
  center: "items-center justify-center",
  top: "items-start justify-center pt-12",
  bottom: "items-end justify-center pb-12",
};

export function ModalScrim({
  onClose,
  closeOnBackdrop = true,
  align = "center",
  className = "",
  children,
}: ModalScrimProps) {
  const onBackdrop = (e: MouseEvent) => {
    if (e.target === e.currentTarget && closeOnBackdrop) onClose?.();
  };
  return (
    <div
      className={`fixed inset-0 z-50 flex ${ALIGN[align]} p-4 ${className}`}
      style={{ background: "rgba(2,2,10,0.72)" }}
      onMouseDown={onBackdrop}
    >
      {children}
    </div>
  );
}

export default ModalScrim;
