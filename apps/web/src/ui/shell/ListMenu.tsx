/**
 * ListMenu — the shared selectable list used by Shop (wares), Inventory (items),
 * Dialogue (choices), and any "pick one of these" surface. Both keyboard- and
 * pointer-driven so it feels native to the pixel-RPG.
 *
 * Behavior:
 *  - ↑/↓ (and W/S) move the highlight; Enter/Space activate; Home/End jump.
 *  - Clicking a row activates it; hovering moves the highlight.
 *  - Disabled rows are skipped by keyboard nav and are not activatable.
 *  - `autoFocus` (default true) grabs keyboard focus on mount so arrows work
 *    immediately — disable it if the surface has another primary focus target.
 *
 * It is UNCONTROLLED by default (tracks its own highlight). Pass `activeIndex`
 * + `onActiveIndexChange` to control it (e.g. to sync a detail pane).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface ListMenuItem<T = unknown> {
  /** Stable key. */
  id: string;
  /** Primary row label (body font). */
  label: ReactNode;
  /** Optional right-aligned annotation (price, count, "NEW", …). */
  trailing?: ReactNode;
  /** Optional second line under the label (muted). */
  hint?: ReactNode;
  /** When true the row is shown dimmed and cannot be selected/activated. */
  disabled?: boolean;
  /** Arbitrary payload returned to onSelect. */
  value?: T;
}

export interface ListMenuProps<T = unknown> {
  items: ListMenuItem<T>[];
  /** Fired when a row is activated (Enter / Space / click). */
  onSelect: (item: ListMenuItem<T>, index: number) => void;
  /** Controlled highlight index. Omit for uncontrolled. */
  activeIndex?: number;
  onActiveIndexChange?: (index: number) => void;
  /** Grab keyboard focus on mount. Default true. */
  autoFocus?: boolean;
  /** aria-label for the listbox. */
  ariaLabel?: string;
  className?: string;
}

function nextEnabled(items: ListMenuItem[], from: number, dir: 1 | -1): number {
  const n = items.length;
  if (n === 0) return -1;
  let i = from;
  for (let step = 0; step < n; step++) {
    i = (i + dir + n) % n;
    if (!items[i]?.disabled) return i;
  }
  return from; // all disabled
}

export function ListMenu<T = unknown>({
  items,
  onSelect,
  activeIndex,
  onActiveIndexChange,
  autoFocus = true,
  ariaLabel,
  className = "",
}: ListMenuProps<T>) {
  const controlled = activeIndex !== undefined;
  const firstEnabled = items.findIndex((it) => !it.disabled);
  const [internal, setInternal] = useState<number>(firstEnabled === -1 ? 0 : firstEnabled);
  const active = controlled ? (activeIndex as number) : internal;
  const ref = useRef<HTMLDivElement>(null);

  const setActive = useCallback(
    (i: number) => {
      if (controlled) onActiveIndexChange?.(i);
      else setInternal(i);
    },
    [controlled, onActiveIndexChange]
  );

  useEffect(() => {
    if (autoFocus) ref.current?.focus();
  }, [autoFocus]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const k = e.key.toLowerCase();
    if (k === "arrowdown" || k === "s") {
      e.preventDefault();
      setActive(nextEnabled(items, active, 1));
    } else if (k === "arrowup" || k === "w") {
      e.preventDefault();
      setActive(nextEnabled(items, active, -1));
    } else if (k === "home") {
      e.preventDefault();
      const i = items.findIndex((it) => !it.disabled);
      if (i !== -1) setActive(i);
    } else if (k === "end") {
      e.preventDefault();
      for (let i = items.length - 1; i >= 0; i--)
        if (!items[i].disabled) {
          setActive(i);
          break;
        }
    } else if (k === "enter" || k === " " || k === "spacebar") {
      e.preventDefault();
      const it = items[active];
      if (it && !it.disabled) onSelect(it, active);
    }
  };

  return (
    <div
      ref={ref}
      role="listbox"
      aria-label={ariaLabel}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className={`flex flex-col gap-1 outline-none ${className}`}
    >
      {items.map((it, i) => {
        const isActive = i === active;
        return (
          <button
            key={it.id}
            role="option"
            aria-selected={isActive}
            aria-disabled={it.disabled || undefined}
            disabled={it.disabled}
            tabIndex={-1}
            onMouseEnter={() => !it.disabled && setActive(i)}
            onClick={() => !it.disabled && onSelect(it, i)}
            className="text-left flex items-center gap-2 px-2.5 py-1.5"
            style={{
              border: "2px solid",
              borderColor: isActive ? "var(--accent)" : "rgba(232,230,216,0.12)",
              background: isActive ? "rgba(255,207,63,0.10)" : "var(--panel2)",
              boxShadow: isActive ? "2px 2px 0 #000" : "none",
              opacity: it.disabled ? 0.4 : 1,
              cursor: it.disabled ? "not-allowed" : "pointer",
            }}
          >
            <span
              className="font-hud text-[10px] w-3 shrink-0"
              style={{ color: "var(--accent)" }}
              aria-hidden
            >
              {isActive ? "▸" : ""}
            </span>
            <span className="flex-1 min-w-0">
              <span className="font-body text-xs block truncate" style={{ color: "var(--ink)" }}>
                {it.label}
              </span>
              {it.hint ? (
                <span className="font-body text-[10px] block truncate" style={{ color: "var(--muted)" }}>
                  {it.hint}
                </span>
              ) : null}
            </span>
            {it.trailing ? (
              <span className="font-hud text-[9px] shrink-0" style={{ color: "var(--muted)" }}>
                {it.trailing}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export default ListMenu;
