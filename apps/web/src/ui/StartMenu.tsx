import type { CSSProperties } from "react";
import { useEffect, useState } from "react";
import {
  DEFAULT_PLAYER_NAME,
  PLAYER_NAME_STORAGE_KEY,
  normalizePlayerName,
  useGame,
} from "../state/store";
import { api } from "../api/client";
import { sfxMenuClose, sfxMenuHover, sfxMenuOpen, sfxMenuSelect, sfxSubmit } from "../lib/sfx";
import { TYPE_COLOR } from "../lib/skills";
import { useIrisTransition } from "./fx/IrisWipe";

// The six debate "elements" (mirrors the elemental palette in index.css).
// Rendered as a pulsing type-ring under the title — pure decoration, no state.
const ELEMENTS = ["LOGOS", "PATHOS", "ETHOS", "CHAOS", "SOCRATIC", "RHETORIC"] as const;
type AvatarMode = (typeof ELEMENTS)[number];
type InfoPanel = "about" | "controls" | "instructions";

// Hidden legacy topic seeds. The backend still requires `topic`, but encounters
// now roll their own topic per battle, so this stays invisible to the player.
const SUGGESTED = [
  "Pineapple belongs on pizza",
  "AI should be open source",
  "Cats are better than dogs",
  "Time travel would do more harm than good",
  "Social media made us lonelier",
  "Pirates would beat ninjas",
];

const INFO_PANELS: Record<InfoPanel, { title: string; kicker: string; lines: string[] }> = {
  about: {
    title: "About",
    kicker: "What this game is",
    lines: [
      "Debate RPG is a monster-catching argument game where every battle is won through stronger reasoning.",
      "You explore a map, challenge AI debaters, capture rivals, and train your party into sharper rhetorical agents.",
      "Each encounter rolls a fresh debate topic, so your run is about adapting, not memorizing one perfect answer.",
    ],
  },
  controls: {
    title: "Controls",
    kicker: "How to play",
    lines: [
      "Enter names your player and Start Run begins the adventure.",
      "Move through the overworld with arrow keys, then collide with enemies to start debates.",
      "In battle, type your argument, choose a rhetorical skill when available, and use capture when an enemy is weak.",
    ],
  },
  instructions: {
    title: "Instructions",
    kicker: "The core loop",
    lines: [
      "Start with a name, explore the overworld, and trigger encounters with wild debaters.",
      "Win arguments by making clear claims, giving evidence, and answering the enemy's logic.",
      "Capture strong opponents, edit party tactics, and train your debaters before harder rematches.",
    ],
  },
};

const AVATAR_MODES: Record<AvatarMode, { title: string; role: string; description: string }> = {
  LOGOS: {
    title: "Logos",
    role: "Evidence tactician",
    description: "Wins by chaining facts, structure, and clean cause-and-effect reasoning.",
  },
  PATHOS: {
    title: "Pathos",
    role: "Emotion striker",
    description: "Turns stories, stakes, and audience feeling into persuasive pressure.",
  },
  ETHOS: {
    title: "Ethos",
    role: "Credibility guard",
    description: "Builds authority, trust, and expert framing before landing the argument.",
  },
  CHAOS: {
    title: "Chaos",
    role: "Reframe disruptor",
    description: "Breaks stale logic, flips assumptions, and forces enemies onto new ground.",
  },
  SOCRATIC: {
    title: "Socratic",
    role: "Question engine",
    description: "Uses sharp questions to expose weak claims and guide the debate's shape.",
  },
  RHETORIC: {
    title: "Rhetoric",
    role: "Style finisher",
    description: "Turns strong wording, rhythm, and framing into decisive closing momentum.",
  },
};

function pickHiddenTopic(): string {
  return SUGGESTED[Math.floor(Math.random() * SUGGESTED.length)] ?? SUGGESTED[0];
}

function readStoredName(): string {
  try {
    return normalizePlayerName(window.localStorage.getItem(PLAYER_NAME_STORAGE_KEY));
  } catch {
    return DEFAULT_PLAYER_NAME;
  }
}

/**
 * StartMenu — the pre-run title screen (WS-G title treatment).
 *
 * An animated title screen that asks for the player's name. Topic selection is
 * intentionally hidden: /api/runs still receives a legacy topic string, while
 * each battle gets its actual topic from the backend encounter flow.
 */
export default function StartMenu() {
  const setRun = useGame((s) => s.setRun);
  const { transition } = useIrisTransition();
  const [playerName, setPlayerName] = useState(readStoredName);
  const [starting, setStarting] = useState(false);
  const [activePanel, setActivePanel] = useState<InfoPanel | null>(null);
  const [avatarOpen, setAvatarOpen] = useState(false);
  const [avatarMode, setAvatarMode] = useState<AvatarMode>("LOGOS");

  useEffect(() => {
    if (!activePanel && !avatarOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeOverlay();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activePanel, avatarOpen]);

  function openInfoPanel(panel: InfoPanel) {
    sfxMenuOpen();
    setActivePanel(panel);
  }

  function openAvatarPanel() {
    sfxMenuOpen();
    setAvatarOpen(true);
  }

  function closeOverlay() {
    sfxMenuClose();
    setActivePanel(null);
    setAvatarOpen(false);
  }

  function selectAvatar(mode: AvatarMode) {
    sfxMenuSelect();
    setAvatarMode(mode);
    setAvatarOpen(false);
  }

  async function startRun() {
    if (starting) return;
    const name = normalizePlayerName(playerName);
    const topic = pickHiddenTopic();
    setStarting(true);
    sfxSubmit();
    try {
      window.localStorage.setItem(PLAYER_NAME_STORAGE_KEY, name);
    } catch {
      /* localStorage is a fallback mirror only. */
    }
    try {
      const run = await api.post<{ id: string; debate_topic: string; player_name?: string }>(
        "/api/runs",
        { topic, player_name: name }
      );
      transition(() => setRun(run.id, run.debate_topic, run.player_name ?? name));
    } catch {
      // /api/runs lands in WS-A; until then just enter the overworld locally.
      transition(() => setRun("local-dev", topic, name));
    }
  }

  return (
    <main
      className="start-menu flex-1 grid place-items-center p-4"
      style={{ "--avatar-color": TYPE_COLOR[avatarMode] } as CSSProperties}
    >
      {/* —— ambient FX layers (decorative, non-interactive) —— */}
      <div className="sm-glow" aria-hidden />
      <div className="sm-stars" aria-hidden />
      <div className="sm-scanlines" aria-hidden />

      <div className="sm-stage w-full max-w-[74rem] text-center">
        <div className="sm-title-block">
          <div className="sm-emblem" aria-hidden>
            ⚔️
          </div>
          <h1 className="sm-title font-display">DEBATE RPG</h1>
          <p className="sm-subtitle font-hud">Out-argue the machines</p>

          <button
            type="button"
            className="sm-avatar-open font-hud"
            style={{ color: TYPE_COLOR[avatarMode] }}
            onClick={openAvatarPanel}
            onMouseEnter={sfxMenuHover}
          >
            <span>Select Avatar</span>
            <strong>{AVATAR_MODES[avatarMode].title}</strong>
          </button>
        </div>

        <div className="pixel-panel sm-panel">
          <div className="sm-console-grid">
            <section className="sm-console-primary" aria-label="Start run">
              <label className="sm-label font-hud" htmlFor="sm-player-name">
                Player Name
              </label>

              <input
                id="sm-player-name"
                className="pixel-field sm-input sm-name-input text-sm"
                value={playerName}
                onChange={(e) => setPlayerName(e.target.value)}
                onBlur={() => setPlayerName((name) => normalizePlayerName(name))}
                onKeyDown={(e) => {
                  if (e.key === "Enter") startRun();
                }}
                placeholder="Enter your name"
                autoComplete="nickname"
                maxLength={32}
              />

              <button
                className="pixel-btn pixel-btn--accent sm-start w-full"
                onClick={startRun}
                disabled={starting}
                aria-busy={starting}
              >
                <span className="sm-start__icon" aria-hidden>
                  ▶
                </span>
                <span>Start Run</span>
                <span className="sm-start__shine" aria-hidden />
              </button>
            </section>

            <section className="sm-console-info" aria-label="Game information">
              <div>
                <div className="sm-label font-hud">Briefing</div>
                <p className="sm-brief font-body">
                  Build a party of AI debaters, win arguments in turn-based battles, capture
                  stronger opponents, and train your team between encounters.
                </p>
              </div>
              <div className="sm-command-row">
                {(Object.keys(INFO_PANELS) as InfoPanel[]).map((panel) => (
                  <button
                    key={panel}
                    type="button"
                    className="pixel-btn sm-command"
                    onClick={() => openInfoPanel(panel)}
                    onMouseEnter={sfxMenuHover}
                  >
                    {INFO_PANELS[panel].title}
                  </button>
                ))}
              </div>
            </section>
          </div>

          <p className="sm-hint font-hud">
            Press <span className="sm-key">Enter</span> to begin
            <span className="caret-blink"> ▌</span>
          </p>
        </div>
      </div>

      {activePanel && (
        <div
          className="sm-overlay"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeOverlay();
          }}
        >
          <section
            className="pixel-panel sm-overlay-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="sm-overlay-title"
          >
            <button
              type="button"
              className="sm-overlay-close"
              aria-label="Close"
              onClick={closeOverlay}
            >
              ×
            </button>
            <div className="sm-overlay-kicker font-hud">{INFO_PANELS[activePanel].kicker}</div>
            <h2 id="sm-overlay-title" className="sm-overlay-title font-display">
              {INFO_PANELS[activePanel].title}
            </h2>
            <div className="sm-overlay-copy">
              {INFO_PANELS[activePanel].lines.map((line) => (
                <p key={line}>{line}</p>
              ))}
            </div>
          </section>
        </div>
      )}

      {avatarOpen && (
        <div
          className="sm-overlay"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeOverlay();
          }}
        >
          <section
            className="pixel-panel sm-overlay-panel sm-avatar-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="sm-avatar-title"
          >
            <button
              type="button"
              className="sm-overlay-close"
              aria-label="Close"
              onClick={closeOverlay}
            >
              ×
            </button>
            <div className="sm-overlay-kicker font-hud">Choose your debate style</div>
            <h2 id="sm-avatar-title" className="sm-overlay-title font-display">
              Select Avatar
            </h2>
            <div className="sm-avatar-grid">
              {ELEMENTS.map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={`sm-avatar-card ${avatarMode === mode ? "sm-avatar-card--on" : ""}`}
                  style={{ borderColor: TYPE_COLOR[mode] }}
                  onClick={() => selectAvatar(mode)}
                  onMouseEnter={sfxMenuHover}
                >
                  <span className="sm-avatar-card__title font-hud" style={{ color: TYPE_COLOR[mode] }}>
                    {AVATAR_MODES[mode].title}
                  </span>
                  <span className="sm-avatar-card__role font-hud">{AVATAR_MODES[mode].role}</span>
                  <span className="sm-avatar-card__copy">{AVATAR_MODES[mode].description}</span>
                </button>
              ))}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
