import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { NPCAnchorView } from "./NPCBehavior";

interface TalkResponse {
  npc_id: string;
  name: string;
  archetype: string;
  text: string;
  cached: boolean;
}

interface NPCDialogueProps {
  runId: string | null;
  npc: NPCAnchorView | null;
  onClose: () => void;
}

export function NPCDialogue({ runId, npc, onClose }: NPCDialogueProps) {
  const [line, setLine] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId || !npc) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setLine("");

    api
      .post<TalkResponse>(`/api/runs/${runId}/npc/${npc.npc_id}/talk`)
      .then((res) => {
        if (cancelled) return;
        setLine(res.text);
      })
      .catch(() => {
        if (cancelled) return;
        setError("The path is silent.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [runId, npc]);

  if (!npc) return null;

  return (
    <div className="absolute inset-x-3 bottom-3 z-40 pointer-events-none">
      <div className="pixel-panel pointer-events-auto max-w-3xl mx-auto p-3">
        <div className="flex items-center gap-2 mb-2">
          <span className="font-hud text-[10px]" style={{ color: "var(--accent)" }}>
            {npc.name || npc.npc_id}
          </span>
          <span className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
            {npc.archetype}
          </span>
          <button className="pixel-btn text-[9px] py-0.5 ml-auto" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="font-body text-sm min-h-10 leading-relaxed" style={{ color: "var(--ink)" }}>
          {loading ? "..." : error ?? line}
        </p>
      </div>
    </div>
  );
}

export default NPCDialogue;
