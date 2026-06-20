/**
 * sfx.ts — tiny retro 8-bit sound-effect layer for the battle screen.
 *
 * Browsers block the WebAudio clock until a user gesture, so synths are created
 * lazily and `Tone.start()` is fired exactly once on the first triggered sound.
 * Every function is a no-op when muted or if Tone throws — audio must NEVER
 * bubble an exception into React.
 */
import * as Tone from "tone";

let enabled = true;
let started = false;

// Synths are created lazily on first use (after a gesture) and cached.
let blipSynth: Tone.Synth | null = null;
let leadSynth: Tone.Synth | null = null;
let polySynth: Tone.PolySynth | null = null;
let membrane: Tone.MembraneSynth | null = null;

export function setSfxEnabled(b: boolean): void {
  enabled = b;
}

/** Ensure the audio context is running. Safe to call repeatedly. */
async function ensureStarted(): Promise<void> {
  if (started) return;
  started = true;
  try {
    // Modest master volume so it isn't jarring on a projector.
    Tone.getDestination().volume.value = -12;
    await Tone.start();
  } catch {
    /* ignore — context may already be running or blocked */
  }
}

function getBlip(): Tone.Synth {
  if (!blipSynth) {
    blipSynth = new Tone.Synth({
      oscillator: { type: "square" },
      envelope: { attack: 0.001, decay: 0.04, sustain: 0, release: 0.02 },
      volume: -16,
    }).toDestination();
  }
  return blipSynth;
}

function getLead(): Tone.Synth {
  if (!leadSynth) {
    leadSynth = new Tone.Synth({
      oscillator: { type: "triangle" },
      envelope: { attack: 0.002, decay: 0.08, sustain: 0.1, release: 0.05 },
      volume: -12,
    }).toDestination();
  }
  return leadSynth;
}

function getPoly(): Tone.PolySynth {
  if (!polySynth) {
    polySynth = new Tone.PolySynth(Tone.Synth, {
      oscillator: { type: "square" },
      envelope: { attack: 0.002, decay: 0.1, sustain: 0.1, release: 0.08 },
      volume: -14,
    }).toDestination();
  }
  return polySynth;
}

function getMembrane(): Tone.MembraneSynth {
  if (!membrane) {
    membrane = new Tone.MembraneSynth({
      pitchDecay: 0.02,
      octaves: 4,
      envelope: { attack: 0.001, decay: 0.2, sustain: 0, release: 0.1 },
      volume: -8,
    }).toDestination();
  }
  return membrane;
}

/** Run an audio gesture safely: no-op when muted, swallow all errors. */
function play(fn: () => void): void {
  if (!enabled) return;
  try {
    void ensureStarted();
    fn();
  } catch {
    /* audio must never throw into React */
  }
}

/** Very short high blip — keypress / typing. C6, 0.05s. */
export function sfxBlip(): void {
  play(() => {
    getBlip().triggerAttackRelease("C6", 0.05);
  });
}

/** Quick two-note up-blip — submitting an argument. E5 → A5. */
export function sfxSubmit(): void {
  play(() => {
    const now = Tone.now();
    const s = getLead();
    s.triggerAttackRelease("E5", 0.06, now);
    s.triggerAttackRelease("A5", 0.08, now + 0.07);
  });
}

/** Punchy low "hit" when a verdict lands — brighter when positive. */
export function sfxHit(positive: boolean): void {
  play(() => {
    const now = Tone.now();
    getMembrane().triggerAttackRelease(positive ? "C3" : "F2", 0.18, now);
    // A short bright accent on a good score, a duller low one otherwise.
    getLead().triggerAttackRelease(positive ? "G4" : "C3", 0.08, now + 0.04);
  });
}

/** Ascending 3-note jingle — successful capture. C5 → E5 → G5. */
export function sfxCapture(): void {
  play(() => {
    const now = Tone.now();
    const s = getLead();
    s.triggerAttackRelease("C5", 0.08, now);
    s.triggerAttackRelease("E5", 0.08, now + 0.09);
    s.triggerAttackRelease("G5", 0.14, now + 0.18);
  });
}

/** Short triumphant jingle — battle won. C5–E5–G5–C6 chord-ish run. */
export function sfxWin(): void {
  play(() => {
    const now = Tone.now();
    const p = getPoly();
    p.triggerAttackRelease("C5", 0.1, now);
    p.triggerAttackRelease("E5", 0.1, now + 0.1);
    p.triggerAttackRelease("G5", 0.1, now + 0.2);
    p.triggerAttackRelease(["C6", "E6"], 0.3, now + 0.32);
  });
}

/** Short descending sad jingle — battle lost. G4 → E4 → C4. */
export function sfxLose(): void {
  play(() => {
    const now = Tone.now();
    const s = getLead();
    s.triggerAttackRelease("G4", 0.12, now);
    s.triggerAttackRelease("E4", 0.12, now + 0.13);
    s.triggerAttackRelease("C4", 0.3, now + 0.27);
  });
}
