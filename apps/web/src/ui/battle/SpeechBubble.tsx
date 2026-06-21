import type { Utterance } from "../../ws/useEncounterStream";
import { useTypewriter } from "./useTypewriter";

const MAX_WORDS = 12;

function truncate(text: string): { short: string; truncated: boolean } {
  // Prefer the first sentence if it's short enough.
  const sentence = text.match(/^[^.!?]+[.!?]/)?.[0] ?? "";
  if (sentence && sentence.split(/\s+/).length <= MAX_WORDS + 3) {
    const truncated = sentence.length < text.length;
    return { short: truncated ? sentence + " …" : sentence, truncated };
  }
  const words = text.split(/\s+/);
  if (words.length <= MAX_WORDS) return { short: text, truncated: false };
  return { short: words.slice(0, MAX_WORDS).join(" ") + " …", truncated: true };
}

export function SpeechBubble({
  utterance,
  isNewest,
  side,
}: {
  utterance: Utterance | null;
  isNewest: boolean;
  side: "player" | "enemy";
}) {
  const { short } = utterance ? truncate(utterance.text) : { short: "" };
  const displayed = useTypewriter(short, isNewest && !!utterance);

  if (!utterance) return null;

  const color = side === "player" ? "var(--party)" : "var(--enemy)";
  const tailLeft = side === "player" ? 20 : undefined;
  const tailRight = side === "enemy" ? 20 : undefined;

  return (
    <div
      style={{
        maxWidth: 260,
        maxHeight: 96,
        overflow: "hidden",
        background: "rgba(14,16,24,0.92)",
        border: `2px solid ${color}`,
        padding: "6px 10px",
        position: "relative",
        boxShadow: "3px 3px 0 rgba(0,0,0,0.6)",
        marginBottom: 8,
      }}
    >
      <p className="font-body text-[11px] leading-snug" style={{ color: "var(--ink)" }}>
        {displayed}
        {isNewest && displayed.length < short.length && (
          <span className="caret-blink">▋</span>
        )}
      </p>
      {/* Tail pointing down toward the character */}
      <div
        style={{
          position: "absolute",
          bottom: -8,
          left: tailLeft,
          right: tailRight,
          width: 0,
          height: 0,
          borderLeft: "6px solid transparent",
          borderRight: "6px solid transparent",
          borderTop: `8px solid ${color}`,
        }}
      />
    </div>
  );
}
