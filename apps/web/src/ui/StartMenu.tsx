import { useEffect, useState } from "react";
import {
  DEFAULT_PLAYER_NAME,
  PLAYER_NAME_STORAGE_KEY,
  normalizePlayerName,
  useGame,
} from "../state/store";
import { api } from "../api/client";
import { sfxMenuClose, sfxMenuHover, sfxMenuOpen, sfxMenuSelect, sfxSubmit } from "../lib/sfx";
import { useIrisTransition } from "./fx/IrisWipe";

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

const RANDOM_NAMES = [
  "Ada",
  "Socrates",
  "Turing",
  "Athena",
  "Cipher",
  "Logos",
  "Nova",
  "Quill",
  "Vector",
  "Rhetor",
  "Thesis",
  "Muse",
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
      "Enter your player name and press Start Game to begin the adventure.",
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

function pickHiddenTopic(): string {
  return SUGGESTED[Math.floor(Math.random() * SUGGESTED.length)] ?? SUGGESTED[0];
}

function pickRandomName(currentName: string): string {
  const current = normalizePlayerName(currentName).toLowerCase();
  const pool = RANDOM_NAMES.filter((name) => name.toLowerCase() !== current);
  return pool[Math.floor(Math.random() * pool.length)] ?? RANDOM_NAMES[0];
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

  useEffect(() => {
    if (!activePanel) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeOverlay();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activePanel]);

  function openInfoPanel(panel: InfoPanel) {
    sfxMenuOpen();
    setActivePanel(panel);
  }

  function closeOverlay() {
    sfxMenuClose();
    setActivePanel(null);
  }

  function generateRandomName() {
    sfxMenuSelect();
    setPlayerName(pickRandomName(playerName));
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
      const run = await api.post<{
        id: string;
        debate_topic: string;
        player_name?: string;
        theme?: string | null;
      }>(
        "/api/runs",
        { topic, player_name: name }
      );
      transition(() => setRun(run.id, run.debate_topic, run.player_name ?? name, run.theme ?? ""));
    } catch {
      // /api/runs lands in WS-A; until then just enter the overworld locally.
      transition(() => setRun("local-dev", topic, name));
    }
  }

  return (
    <main className="start-menu flex-1 grid place-items-center p-4">
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
            className="sm-title-start font-hud"
            onClick={startRun}
            onMouseEnter={sfxMenuHover}
            disabled={starting}
            aria-busy={starting}
          >
            <span>Ready</span>
            <strong>Start Game</strong>
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
                type="button"
                className="pixel-btn pixel-btn--accent sm-random w-full"
                onClick={generateRandomName}
                disabled={starting}
                onMouseEnter={sfxMenuHover}
              >
                <span className="sm-random__icon" aria-hidden>
                  ✦
                </span>
                <span>Random Name</span>
                <span className="sm-random__shine" aria-hidden />
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

    </main>
  );
}
