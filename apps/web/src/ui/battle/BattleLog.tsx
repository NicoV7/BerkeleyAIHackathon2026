import { useEffect, useRef } from "react";
import type { Utterance } from "../../ws/useEncounterStream";
import { useTypewriter } from "./useTypewriter";

function LogLine({
  u,
  liveNames,
  isNewest,
}: {
  u: Utterance;
  liveNames: Record<string, string>;
  isNewest: boolean;
}) {
  const isParty = u.actor_role === "party";
  const isJudge = u.actor_role === "judge";
  const nameColor = isJudge ? "var(--accent)" : isParty ? "var(--party)" : "var(--enemy)";
  const textColor = isJudge ? "var(--accent)" : "var(--ink)";
  const text = useTypewriter(u.text, isNewest);

  return (
    <div className="leading-relaxed border-b border-white/5 pb-1.5">
      <div className="flex items-center gap-1 mb-0.5">
        <span className="font-hud text-[10px]" style={{ color: nameColor }}>
          {liveNames[u.actor_id] ?? u.actor_id}
        </span>
        {u.skill_used && (
          <span
            className="font-hud text-[8px] px-0.5"
            style={{ background: "rgba(255,255,255,0.08)", color: "var(--muted)" }}
          >
            {u.skill_used}
          </span>
        )}
        <span className="ml-auto font-hud text-[9px]" style={{ color: "var(--muted)" }}>
          T{u.turn}
        </span>
      </div>
      <p
        className="font-body text-[14px] leading-relaxed whitespace-pre-wrap break-words"
        style={{ color: textColor, opacity: isJudge ? 0.85 : 1 }}
      >
        {text}
        {isNewest && text.length < u.text.length && <span className="caret-blink">▋</span>}
      </p>
    </div>
  );
}

export function BattleLog({
  transcript,
  liveNames,
  newestTurn,
}: {
  transcript: Utterance[];
  liveNames: Record<string, string>;
  newestTurn: Utterance | null;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript.length]);

  return (
    <div className="h-full flex-1 overflow-y-auto flex flex-col min-h-0">
      <div
        className="font-hud text-[11px] uppercase tracking-wider px-2 pt-2 pb-1.5 shrink-0 sticky top-0"
        style={{ color: "var(--muted)", background: "rgba(14,16,24,0.96)" }}
      >
        Log ({transcript.length})
      </div>
      <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-2">
        {transcript.length === 0 && (
          <p className="font-body text-[14px] italic pt-2" style={{ color: "var(--muted)" }}>
            Debate log will appear here…
          </p>
        )}
        {transcript.map((u, i) => (
          <LogLine
            key={`${u.turn}-${u.actor_id}-${i}`}
            u={u}
            liveNames={liveNames}
            isNewest={u === newestTurn}
          />
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}
