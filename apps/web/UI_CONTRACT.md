# UI Contract & Nav Model (WS-0-UI)

This is the shared frame every later surface (Inventory, Quests, Camp, Shop,
Dialogue) builds INTO. WS-0-UI owns the shell, the nav model, the reusable
primitives, and the state vocabulary. **If you are building a surface: read this
first, then import from `src/ui/shell` instead of inventing your own chrome.**

- Design tokens live in `src/index.css` (`:root` vars + `.pixel-*` classes).
- Reusable primitives live in `src/ui/shell/` (barrel: `src/ui/shell/index.ts`).
- Overlay state lives in `src/state/store.ts` (`overlay`, `openOverlay`, `closeOverlay`).
- Intro content lives in `src/content/introScript.ts`.

---

## 1. Nav model (the agreed model from review)

```
┌──────────────────────────────────────────────────────────────┐
│  GLOBAL HUD BAR  (persistent in overworld)                    │
│  ⚔️ DEBATE RPG · api · player · theme       [Adventure menu]  │
│  [overworld][encounter][party][training]    🎒Items 📜Quests 🗺Map │
└──────────────────────────────────────────────────────────────┘
        │ Adventure menu opens overlay surfaces (no screen change)
        ▼
   Inventory / Quests / Map  → ModalScrim + MenuPanel (overlay)

   Camp / Shop               → DIEGETIC: entered by walking into a world POI
   Party                     → always viewable (top tab / HUD)
   Encounter                 → ONLY during an active battle
   Dialogue                  → HUD drawer over the overworld (NPC talk)
```

Principles:

1. **Persistent HUD bar.** The header + nav row are always visible in the
   overworld. They are replaced by a "locked" banner during battle and by a
   "summon required" banner during the gacha gate (see `App.tsx`).
2. **Adventure menu** (`AdventureMenu`, lives in the HUD nav row) opens the
   non-diegetic surfaces — **Inventory, Quests, Map** — as overlays via the
   store `overlay` field. Opening an overlay does **not** change `screen`.
3. **Camp & Shop are diegetic.** They are entered by walking into a world POI in
   the Phaser overworld, **not** from the Adventure menu. They therefore do
   **not** appear in the `Overlay` union. Their authors decide whether they
   present as a full-screen React takeover or an overlay (recommended: overlay
   via `ModalScrim` + `MenuPanel`, same as the menu surfaces).
4. **Party is always viewable** (top tab today; will remain reachable after the
   WS-6 tab restructure).
5. **Encounter is battle-only.** It is reachable today as a top tab, but WS-6
   will remove that tab so the only entry is an actual encounter.

---

## 2. Presentation: full-screen vs panel vs modal vs HUD drawer

| Surface          | Presentation          | Pauses Phaser? | Owner |
|------------------|-----------------------|----------------|-------|
| Overworld        | Full-screen (Phaser)  | n/a (is Phaser)| WS-A  |
| Encounter/Battle | Full-screen (React)   | Yes — replaces canvas region | WS-B/C |
| Party            | Full-screen (React)   | Yes — replaces canvas region | WS-E |
| Training         | Full-screen (React)   | Yes — replaces canvas region | WS-F |
| **Inventory**    | **Overlay modal**     | No — floats over overworld | WS-2 |
| **Quests**       | **Overlay modal**     | No — floats over overworld | WS-2 |
| **Map**          | **Overlay modal**     | No — floats over overworld | WS-2 |
| **Camp**         | Overlay modal (rec.)  | No — diegetic POI | camp author |
| **Shop**         | Overlay modal (rec.)  | No — diegetic POI | shop author |
| **Dialogue**     | **HUD drawer** (bottom)| No — overlays overworld | WS-3 |

Definitions:

- **Full-screen** — occupies the `<main>` region; the active `screen`. The
  Phaser canvas is unmounted while a non-overworld screen is active (see
  `ScreenPanel` in `App.tsx`), so it is effectively paused.
- **Overlay modal** — a `ModalScrim` (dimmed backdrop, z-50) hosting a
  `MenuPanel`. The overworld (canvas or current screen) stays mounted behind it.
- **HUD drawer** — a panel anchored to an edge of the overworld (e.g. the NPC
  dialogue box anchored bottom, z-40), pointer-events scoped to the panel.
- **HUD chrome** — non-interactive overlays drawn on the canvas (minimap, stat
  bars, WASD keys) — `pointer-events:none`, z-10.

---

## 3. React-vs-Phaser overlay rule

**The Phaser overworld canvas only renders while `screen === "overworld"`.**
`ScreenPanel` swaps between `<Overworld/>` (Phaser) and the React screens, so:

- React **full-screen** screens **replace** Phaser (canvas is unmounted →
  effectively paused). Do not assume the canvas exists off the overworld.
- React **overlay modals / HUD drawers** render **on top of** the live Phaser
  canvas. They do NOT unmount it. Phaser keeps running underneath.
- **Input isolation:** when an overlay/drawer is open over the overworld, it
  should swallow the keys it uses (Esc, arrows for `ListMenu`) so they don't
  also drive the Phaser player. `MenuPanel` already `stopPropagation`s Esc;
  `ListMenu` `preventDefault`s its nav keys. If your overlay needs full input
  capture, render a `ModalScrim` (it covers the canvas and intercepts pointer
  events on the backdrop).
- **Never mount a live 2D `<canvas>` in the DOM over the WebGL canvas** — it
  corrupts Phaser's compositing (see `OverworldHud` minimap, which renders
  offscreen to a data-URL `<img>`). Use `<img>`/CSS, not a live canvas, in HUD.

---

## 4. Z-order

| Layer | z-index | Examples |
|-------|---------|----------|
| Phaser canvas | (base) | overworld |
| HUD chrome (non-interactive) | 10 | minimap, stat bars, WASD, hint text |
| HUD drawer (interactive) | 40 | NPC dialogue box |
| Overlay scrim + modal | 50 | Inventory/Quests/Map, Camp/Shop |
| Iris transition | 9999 | screen-change wipe |

Rules: overlays (50) always sit above HUD (10–40). Nothing except the iris
transition (9999) goes above an overlay. Keep new surfaces within these bands —
do not invent ad-hoc z-indexes above 50.

---

## 5. Universal back / close behavior

One grammar, everywhere — implemented by `MenuPanel` + `ModalScrim`:

- **Close** (✕, top-right of `MenuPanel`) — dismisses the surface entirely. For
  Adventure-menu overlays this calls `closeOverlay()` (clears `overlay`).
- **Back** (← , top-left of `MenuPanel`, optional) — pops one level WITHIN a
  surface (e.g. Shop detail → Shop list). Only render it when there is an
  in-surface level to pop; otherwise omit it and rely on Close.
- **Escape** — closes the top-most surface. `MenuPanel` handles Esc when
  `onClose` is set (`closeOnEsc`, default true) and `stopPropagation`s it so it
  doesn't leak to Phaser/parent.
- **Click-outside** — `ModalScrim` closes on backdrop click
  (`closeOnBackdrop`, default true).
- **During battle** (`battleLocked`) — global nav AND overlays are suppressed.
  The only exit is the in-battle Flee / win / lose. `setEncounter` clears
  `overlay` so nothing floats into a battle.

---

## 6. Screen lifecycle

- `screen` (store) selects the full-screen surface; `setScreen` changes it,
  ideally wrapped in `transition()` (iris wipe) when the user navigates.
- `overlay` (store) selects the floating Adventure-menu surface; independent of
  `screen`. `openOverlay`/`closeOverlay` toggle it.
- Run boundary: `setRun` resets `screen` to `overworld` and clears `overlay`.
- Battle boundary: entering a battle (`setEncounter(id)`) sets `screen` to
  `encounter`, `battleLocked` true, and clears `overlay`. Leaving clears the lock.
- Gacha gate: when a run loads with an empty party (`needsGacha`), the HUD nav is
  replaced by a banner and `<main>` shows `GachaScreen`; overlays are suppressed
  until the first pull completes.
- Surfaces should be self-contained: fetch on mount, clean up on unmount, and
  not assume sibling surfaces are mounted.

---

## 7. Per-surface STATE MATRIX

Every surface MUST handle these four states using the shared primitives
(`LoadingState`, `EmptyState`, `ErrorState`). Render them **inside** your
`MenuPanel` body (they don't draw their own panel chrome). Copy below is the
contract — match it so the game reads consistently.

| Surface | Loading | Empty | Error | Disabled |
|---|---|---|---|---|
| **Inventory** | `LoadingState label="Reading your pack"` | `EmptyState 🎒 "Your pack is empty" — "Loot and rewards land here. Win debates to fill it."` | `ErrorState "Couldn't open your pack."` + Retry | An item the player can't use here is shown via `ListMenu` `disabled` (dimmed, not activatable). |
| **Quests** | `LoadingState label="Consulting the log"` | `EmptyState 📜 "No quests yet" — "Talk to NPCs to take on quests. Your first comes from {introNpc}."` | `ErrorState "The log is illegible."` + Retry | Completed/locked quests shown disabled; active quests selectable. |
| **Map** | `LoadingState label="Charting the region"` | `EmptyState 🗺 "Nothing charted yet" — "Explore to reveal the map."` | `ErrorState "The map is torn."` + Retry | Unreachable/locked POIs shown disabled. |
| **Camp** | `LoadingState label="Setting up camp"` | `EmptyState 🏕 "Camp is quiet" — "Rest and manage your party here."` | `ErrorState "You can't make camp here."` + (no retry; close) | Rest/heal actions disabled when already rested or mid-cooldown. |
| **Shop** | `LoadingState label="Browsing the wares"` | `EmptyState 🪙 "Out of stock" — "Come back later — the merchant is restocking."` | `ErrorState "The merchant turns you away."` + Retry | Items the player can't afford shown disabled (dimmed) with the price still visible in `trailing`. |
| **Dialogue** | line area shows animated "…" (or `LoadingState` for first fetch) | n/a (always has at least one line) | `ErrorState "The path is silent."` (matches existing `NPCDialogue`) | Choices that are unavailable shown as disabled `ListMenu` rows. |

Notes:
- **Loading** should not flash for sub-150ms fetches; prefer optimistic render if
  you already have cached data.
- **Error** copy stays short + in-world; put technical detail in `ErrorState`'s
  `detail` prop, not the headline.
- **Disabled** is a per-row concern handled by `ListMenu` `disabled` — keep the
  affordance visible (dimmed) rather than hiding it, so the player understands
  the option exists.

---

## 8. Reusable primitives (import from `src/ui/shell`)

| Primitive | Purpose | Key props |
|---|---|---|
| `MenuPanel` | Titled container for any surface; owns title bar + Back/Close/Esc. | `title`, `subtitle?`, `onBack?`, `onClose?`, `closeOnEsc?`, `footer?`, `maxWidthClassName?` |
| `ListMenu<T>` | Keyboard+click selectable list (shop/inventory/quests/dialogue). | `items: ListMenuItem<T>[]`, `onSelect(item,index)`, `activeIndex?`, `onActiveIndexChange?`, `autoFocus?`, `ariaLabel?` |
| `ListMenuItem<T>` | One row: `id`, `label`, `trailing?`, `hint?`, `disabled?`, `value?` | — |
| `EmptyState` | "Nothing here yet" placeholder. | `icon?`, `title`, `message?`, `actionLabel?`, `onAction?` |
| `LoadingState` | "Fetching…" placeholder (animated dots). | `label?` |
| `ErrorState` | Failure placeholder. | `message?`, `detail?`, `onRetry?`, `retryLabel?` |
| `ModalScrim` | Dimmed backdrop (z-50) that centers + hosts a `MenuPanel`. | `onClose?`, `closeOnBackdrop?`, `align?` |
| `AdventureMenu` | HUD button group → opens Inventory/Quests/Map overlays. | `className?` |
| `OverlayHost` | Renders the active overlay surface (has Wave-2 placeholders). | — |

Typical surface skeleton (e.g. a Shop):

```tsx
import { MenuPanel, ListMenu, LoadingState, EmptyState, ErrorState } from "@/ui/shell";

function Shop({ onClose }: { onClose: () => void }) {
  // ...fetch wares...
  return (
    <MenuPanel title="Shop" onClose={onClose} footer={<button className="pixel-btn pixel-btn--accent text-[10px]">Buy</button>}>
      {loading ? <LoadingState label="Browsing the wares" />
        : error ? <ErrorState message="The merchant turns you away." onRetry={refetch} />
        : wares.length === 0 ? <EmptyState icon="🪙" title="Out of stock" message="Come back later." />
        : <ListMenu items={wares} onSelect={buy} ariaLabel="Wares" />}
    </MenuPanel>
  );
}
```

---

## 9. Button grammar

- **Default action** → `pixel-btn`. **Primary/confirm** → `pixel-btn pixel-btn--accent`
  (gold). **Player-side** → `pixel-btn--party` (cyan). **Enemy/danger** →
  `pixel-btn--enemy` (rose).
- **Sizing** by context: HUD/inline `text-[9px] py-0.5`; in-panel `text-[10px]`;
  hero/CTA `text-xs`+. All button text is UPPERCASE HUD font (handled by
  `.pixel-btn`).
- **Iconography**: a leading glyph + space + label (e.g. `🎒 Items`, `← Back`,
  `✕`). Keep one glyph max.
- **Close = `✕`** (top-right), **Back = `← Label`** (top-left). One primary
  action per panel footer; secondary actions to its left.
- **Disabled** uses the native `disabled` attribute (`.pixel-btn:disabled` dims
  to 0.4 + `not-allowed`). Don't fake disabled with opacity alone.

---

## 10. Design tokens (pixel-RPG) — match these

Source of truth is `src/index.css`. Quick reference:

- **Type**: display `--font-display` (Press Start 2P) for big titles only; HUD
  `--font-hud` (Silkscreen, uppercase, letter-spacing) for labels/buttons; body
  `--font-body` (JetBrains Mono) for prose. Common sizes: `text-[8px]`/`[9px]`
  (HUD), `text-[10px]` (buttons), `text-xs`/`text-sm` (body).
- **Spacing**: panel padding `p-2`/`p-3`; row gaps `gap-1`/`gap-2`; tight,
  grid-aligned. No rounded corners (globally forced to `border-radius:0`).
- **Border + shadow** (the pixel look): panels `border: 3px solid rgba(232,230,216,0.18)`
  + `box-shadow: 4px 4px 0 #000` (`.pixel-panel`); buttons 2px border + `3px 3px 0 #000`
  hard offset shadow, press = `translate(3px,3px)` (`.pixel-btn`); insets
  `.pixel-inset`.
- **Palette**: `--bg #0e1018`, `--panel #1a1d2e`, `--panel2 #11131f`,
  `--ink #e8e6d8`, `--muted #8a8fa3`, `--accent/gold #ffcf3f`,
  `--party/cyan #5cc8ff`, `--enemy/rose #ff5d6c`, `--win #6ee787`,
  `--danger #ff5d6c`. Elemental (debate-type): `--logos --pathos --ethos
  --chaos --socratic --rhetoric`. Reference via `var(--token)`, never hardcode
  hex in components.

---

## 11. Intro flow (content contract)

`src/content/introScript.ts` is DATA + TYPES only (no React, no API). WS-3 wires
it to the first NPC:

- Render `INTRO_SCRIPT.lines` in order through the Dialogue surface (HUD drawer),
  advancing on click/Enter.
- At the end, render `INTRO_SCRIPT.choices` via `ListMenu` under
  `INTRO_SCRIPT.choicePrompt`.
- The choice with `effect: "accept_quest_and_pull"` must: grant the first quest
  (`FIRST_QUEST_ID`) and trigger the first gacha pull. `App.tsx` already gates an
  empty party through `GachaScreen` (`needsGacha`), so accepting can simply route
  the player toward the existing gacha gate (or trigger the pull directly).
- `effect: "repeat_explanation"` re-shows the explanation lines;
  `effect: "decline"` dismisses (the player can return to the NPC later).
- Empty-Party copy is `EMPTY_PARTY_COPY` (`title`, `message`, `cta`) — surfaces
  that show an empty party (PartyScreen, future Party overlay) should render it
  verbatim so the new-player funnel is consistent.

---

## 12. TODO — planned tab removal (WS-6 owns this restructure)

The current top-tab strip in `App.tsx` (`overworld | encounter | party |
training`) is a Wave-2 stopgap. **WS-0-UI is NOT removing it** — WS-6 owns the
screen restructure. Planned end state + the audit WS-6 must do:

- **Remove the `encounter` tab.** Encounter must be reachable ONLY via an actual
  battle (`setEncounter(id)`), never as a free-nav tab. Audit every
  `setScreen("encounter")` caller — there should be none after this; encounter
  is set exclusively through `setEncounter`.
- **Remove the `training` tab** (or relocate training behind a diegetic entry /
  Adventure-menu entry, per WS-6's design). Audit `setScreen("training")`
  callers and reroute them.
- **Keep `overworld` and `party`** reachable (party may move to the Adventure
  menu or a dedicated HUD button — WS-6 decides; if it becomes an overlay, add
  `"party"` to the `Overlay` union in `store.ts` and render it in `OverlayHost`).
- **`setScreen` caller audit checklist** (run before/after the restructure):
  `grep -rn 'setScreen(' apps/web/src` and `grep -rn 'screen ===' apps/web/src`
  — every caller must still resolve to a screen that has an entry point in the
  new nav. The `Screen` union in `store.ts` is the source of truth; prune unused
  members there once the tabs are gone.
- The `Overlay` union + `openOverlay`/`closeOverlay`/`OverlayHost` are the
  forward-compatible home for any surface WS-6 decides should float rather than
  take over the screen.
