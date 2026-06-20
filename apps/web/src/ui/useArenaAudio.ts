/**
 * useArenaAudio — short synthesized "sting" cues for key arena beats.
 *
 * Purpose (taste decision T-D1, Wave 1): a noisy demo room is text-hostile;
 * a single audio cue on KO / capture / "winner" is the only true head-turn.
 * This hook plays a SHORT sound on those beats and nothing else.
 *
 * Design constraints:
 *  - **Mute default-OFF**: muted by default so it never blares unexpectedly.
 *    The caller flips `muted` (persisted to localStorage) to opt in.
 *  - **No assets / no network**: stings are synthesized live via WebAudio
 *    (a tiny envelope + oscillator chord). An optional spoken word ("Winner!")
 *    can be layered via the browser `speechSynthesis` API when available.
 *  - **Additive + safe**: every browser call is guarded; on any failure the
 *    hook degrades to silence. It never throws into the render tree.
 *  - **Edge-triggered**: callers pass the current beat; the hook fires only on
 *    a *change* to a sting-worthy beat, so re-renders don't re-trigger sound.
 *
 * Usage:
 *   const audio = useArenaAudio();              // muted by default
 *   audio.setMuted(false);                      // user opts in
 *   audio.onBeat(phase);                        // call on phase transitions
 *
 * The hook is intentionally framework-light: no context, no providers.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/** Beats that warrant an audible sting. */
export type ArenaBeat = "ko" | "win" | "loss" | "capture";

/** localStorage key for the persisted mute preference (default-off). */
const MUTE_STORAGE_KEY = "arena.audio.muted";

/** Public surface of the hook. */
export interface ArenaAudio {
  /** True when audio is suppressed. Defaults to true (off). */
  muted: boolean;
  /** Toggle / set the mute preference (persisted to localStorage). */
  setMuted: (muted: boolean) => void;
  toggleMuted: () => void;
  /** Play the sting for a specific beat immediately (respects mute). */
  play: (beat: ArenaBeat) => void;
  /**
   * Edge-triggered convenience: pass the latest encounter phase string. Fires
   * the matching sting only when the mapped beat *changes*. Unknown / quiet
   * phases reset the edge so the next real beat re-triggers cleanly.
   */
  onBeat: (phase: string | null | undefined) => void;
}

/** Map an encounter phase to its sting beat (or null for "no sound"). */
function phaseToBeat(phase: string | null | undefined): ArenaBeat | null {
  switch (phase) {
    case "won":
      // KO + winner: the marquee head-turn moment.
      return "win";
    case "lost":
      return "loss";
    case "capturable":
      return "capture";
    default:
      return null;
  }
}

/** Read the persisted mute preference, defaulting to muted (off). */
function readMutedPref(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(MUTE_STORAGE_KEY);
    // Anything other than an explicit "false" stays muted (default-off).
    return raw !== "false";
  } catch {
    return true;
  }
}

export function useArenaAudio(): ArenaAudio {
  const [muted, setMutedState] = useState<boolean>(readMutedPref);

  // A single lazily-created AudioContext, reused for every sting.
  const ctxRef = useRef<AudioContext | null>(null);
  // Last beat fired via onBeat(), for edge-triggering.
  const lastBeatRef = useRef<ArenaBeat | null>(null);
  // Mirror muted in a ref so play() captured in callbacks reads current value.
  const mutedRef = useRef<boolean>(muted);
  useEffect(() => {
    mutedRef.current = muted;
  }, [muted]);

  const setMuted = useCallback((next: boolean) => {
    setMutedState(next);
    try {
      window.localStorage.setItem(MUTE_STORAGE_KEY, next ? "true" : "false");
    } catch {
      /* storage unavailable — keep in-memory state only */
    }
  }, []);

  const toggleMuted = useCallback(() => {
    setMuted(!mutedRef.current);
  }, [setMuted]);

  /** Lazily obtain (and resume) the shared AudioContext, or null if unsupported. */
  const getCtx = useCallback((): AudioContext | null => {
    if (typeof window === "undefined") return null;
    try {
      if (!ctxRef.current) {
        const Ctor: typeof AudioContext | undefined =
          window.AudioContext ??
          (window as unknown as { webkitAudioContext?: typeof AudioContext })
            .webkitAudioContext;
        if (!Ctor) return null;
        ctxRef.current = new Ctor();
      }
      // A user gesture may be required to start audio; resume() is a no-op if running.
      if (ctxRef.current.state === "suspended") {
        void ctxRef.current.resume();
      }
      return ctxRef.current;
    } catch {
      return null;
    }
  }, []);

  /**
   * Synthesize a short tone: a frequency-ramped oscillator through a quick
   * attack/decay gain envelope. Multiple calls layer into a chord/arpeggio.
   */
  const tone = useCallback(
    (
      ctx: AudioContext,
      opts: {
        freq: number;
        endFreq?: number;
        start?: number;
        dur?: number;
        type?: OscillatorType;
        gain?: number;
      }
    ) => {
      const {
        freq,
        endFreq,
        start = 0,
        dur = 0.18,
        type = "square",
        gain = 0.18,
      } = opts;
      const t0 = ctx.currentTime + start;
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, t0);
      if (endFreq != null) {
        osc.frequency.exponentialRampToValueAtTime(
          Math.max(1, endFreq),
          t0 + dur
        );
      }
      // Fast attack, exponential-ish decay to near-zero.
      env.gain.setValueAtTime(0.0001, t0);
      env.gain.linearRampToValueAtTime(gain, t0 + 0.012);
      env.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(env);
      env.connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + dur + 0.02);
    },
    []
  );

  /** Optional spoken cue layered on top of the chime (best-effort). */
  const speak = useCallback((text: string) => {
    if (typeof window === "undefined") return;
    try {
      const synth = window.speechSynthesis;
      if (!synth || typeof window.SpeechSynthesisUtterance !== "function") return;
      const u = new window.SpeechSynthesisUtterance(text);
      u.rate = 1.05;
      u.pitch = 1.1;
      u.volume = 0.9;
      synth.cancel(); // never queue / overlap stale announcements
      synth.speak(u);
    } catch {
      /* speech unsupported — chime alone is enough */
    }
  }, []);

  const play = useCallback(
    (beat: ArenaBeat) => {
      if (mutedRef.current) return;
      const ctx = getCtx();
      if (!ctx) return;

      switch (beat) {
        case "win":
        case "ko": {
          // Bright rising triad fanfare — the "winner" head-turn.
          tone(ctx, { freq: 523.25, dur: 0.14, gain: 0.16 }); // C5
          tone(ctx, { freq: 659.25, start: 0.1, dur: 0.14, gain: 0.16 }); // E5
          tone(ctx, {
            freq: 783.99,
            endFreq: 1046.5,
            start: 0.2,
            dur: 0.28,
            gain: 0.2,
          }); // G5 -> C6 swoop
          speak("Winner!");
          break;
        }
        case "capture": {
          // Two ascending chimes — "you can catch it now".
          tone(ctx, { freq: 880, dur: 0.12, type: "triangle", gain: 0.16 }); // A5
          tone(ctx, {
            freq: 1174.66,
            start: 0.11,
            dur: 0.18,
            type: "triangle",
            gain: 0.16,
          }); // D6
          break;
        }
        case "loss": {
          // Descending minor drop — somber, short.
          tone(ctx, { freq: 392, endFreq: 196, dur: 0.4, type: "sawtooth", gain: 0.16 });
          break;
        }
        default:
          break;
      }
    },
    [getCtx, tone, speak]
  );

  const onBeat = useCallback(
    (phase: string | null | undefined) => {
      const beat = phaseToBeat(phase);
      // Reset the edge when we return to a quiet phase so the next real beat fires.
      if (beat == null) {
        lastBeatRef.current = null;
        return;
      }
      if (lastBeatRef.current === beat) return; // already announced this beat
      lastBeatRef.current = beat;
      play(beat);
    },
    [play]
  );

  // Tear down the AudioContext on unmount.
  useEffect(() => {
    return () => {
      try {
        ctxRef.current?.close();
      } catch {
        /* ignore */
      }
      ctxRef.current = null;
    };
  }, []);

  return useMemo<ArenaAudio>(
    () => ({ muted, setMuted, toggleMuted, play, onBeat }),
    [muted, setMuted, toggleMuted, play, onBeat]
  );
}

export default useArenaAudio;
