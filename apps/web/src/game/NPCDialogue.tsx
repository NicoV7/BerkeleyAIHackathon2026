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

interface QuestResponse {
  quest: {
    title: string;
    description: string;
    reward: string;
    target: string;
  } | null;
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
  const [quest, setQuest] = useState<QuestResponse["quest"]>(null);
  const [questLoading, setQuestLoading] = useState(false);
  const [questError, setQuestError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId || !npc) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setLine("");
    setQuest(null);
    setQuestError(null);
    setQuestLoading(false);

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

  const canOfferQuest = npc.archetype === "merchant" || npc.archetype === "quest_giver";

  const acceptQuest = () => {
    if (!runId || !npc || questLoading) return;
    setQuestLoading(true);
    setQuestError(null);
    api
      .post<QuestResponse>(`/api/runs/${runId}/quest/accept`, { npc_id: npc.npc_id })
      .then((res) => {
        setQuest(res.quest);
        if (!res.quest) setQuestError("No work nearby.");
      })
      .catch(() => setQuestError("The notice board is blank."))
      .finally(() => setQuestLoading(false));
  };

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
        {canOfferQuest ? (
          <div
            className="mt-3 border-t pt-3"
            style={{ borderColor: "rgba(232,230,216,0.16)" }}
          >
            {quest ? (
              <div className="font-body text-xs leading-relaxed" style={{ color: "var(--ink)" }}>
                <div className="font-hud text-[9px] mb-1" style={{ color: "var(--accent)" }}>
                  {quest.title}
                </div>
                <div>{quest.description}</div>
                <div className="mt-1" style={{ color: "var(--muted)" }}>
                  Reward: {quest.reward}
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <button
                  className="pixel-btn pixel-btn--accent text-[9px] py-1"
                  onClick={acceptQuest}
                  disabled={questLoading}
                >
                  {questLoading ? "Checking..." : "Take quest"}
                </button>
                {questError ? (
                  <span className="font-body text-xs" style={{ color: "var(--muted)" }}>
                    {questError}
                  </span>
                ) : null}
              </div>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default NPCDialogue;
