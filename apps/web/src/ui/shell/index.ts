/**
 * Shared UI shell primitives (WS-0-UI). Import from here so every later
 * surface (Inventory, Quests, Camp, Shop, Dialogue) matches the same frame,
 * list grammar, and loading/empty/error vocabulary.
 *
 *   import { MenuPanel, ListMenu, EmptyState, LoadingState, ErrorState, ModalScrim }
 *     from "@/ui/shell";
 *
 * See apps/web/UI_CONTRACT.md for the nav model, presentation rules, and the
 * per-surface state matrix these primitives implement.
 */
export { MenuPanel } from "./MenuPanel";
export type { MenuPanelProps } from "./MenuPanel";

export { ListMenu } from "./ListMenu";
export type { ListMenuProps, ListMenuItem } from "./ListMenu";

export { EmptyState } from "./EmptyState";
export type { EmptyStateProps } from "./EmptyState";

export { LoadingState } from "./LoadingState";
export type { LoadingStateProps } from "./LoadingState";

export { ErrorState } from "./ErrorState";
export type { ErrorStateProps } from "./ErrorState";

export { ModalScrim } from "./ModalScrim";
export type { ModalScrimProps } from "./ModalScrim";

export { AdventureMenu } from "./AdventureMenu";
export { OverlayHost } from "./OverlayHost";
