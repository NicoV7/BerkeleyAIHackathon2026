import { useEffect, useState } from "react";
import { api } from "../api/client";

export interface FigureSummary {
  id: string;
  name: string;
  bio: string;
  sprite: string;
  alignment: string;
  recruited: boolean;
  signature_topics: string[];
  recruit_trial_topic: string;
}

export interface SummonResult {
  summoned: boolean;
  figure: FigureSummary;
  voice: string;
  turn_prompt: string;
}

interface SummonOverlayProps {
  runId: string | null;
  open: boolean;
  topic?: string;
  onClose: () => void;
  onSummoned?: (result: SummonResult) => void;
}

export function SummonOverlay({
  runId,
  open,
  topic,
  onClose,
  onSummoned,
}: SummonOverlayProps) {
  const [figures, setFigures] = useState<FigureSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summoning, setSummoning] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !runId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get<{ figures: FigureSummary[] }>(`/api/runs/${runId}/figures`)
      .then((res) => {
        if (!cancelled) setFigures(res.figures);
      })
      .catch(() => {
        if (!cancelled) setError("Roster unavailable.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, runId]);

  if (!open) return null;

  async function summon(figure: FigureSummary) {
    if (!runId || !figure.recruited || summoning) return;
    setSummoning(figure.id);
    setError(null);
    try {
      const result = await api.post<SummonResult>(`/api/runs/${runId}/summon`, {
        figure_id: figure.id,
        battle_state: { topic },
      });
      onSummoned?.(result);
      onClose();
    } catch {
      setError("Summon failed.");
    } finally {
      setSummoning(null);
    }
  }

  return (
    <div className="absolute inset-0 z-50 grid place-items-center p-4" style={{ background: "rgba(0,0,0,0.62)" }}>
      <div className="pixel-panel w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
        <div className="flex items-center gap-2 p-3" style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}>
          <span className="font-hud text-xs" style={{ color: "var(--accent)" }}>
            Summons
          </span>
          <button className="pixel-btn text-[9px] py-0.5 ml-auto" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="p-3 overflow-y-auto grid gap-2 sm:grid-cols-2">
          {loading && (
            <div className="font-body text-sm" style={{ color: "var(--muted)" }}>
              Loading...
            </div>
          )}
          {error && (
            <div className="font-body text-sm" style={{ color: "var(--danger)" }}>
              {error}
            </div>
          )}
          {!loading &&
            figures.map((figure) => (
              <button
                key={figure.id}
                disabled={!figure.recruited || summoning != null}
                onClick={() => summon(figure)}
                className="pixel-inset text-left p-2 disabled:opacity-40"
                style={{
                  borderColor: figure.recruited ? "var(--party)" : "rgba(232,230,216,0.12)",
                }}
              >
                <div className="flex gap-2">
                  <div
                    className="w-12 h-12 shrink-0 pixel-inset"
                    style={{
                      backgroundImage: figure.sprite ? `url(${figure.sprite})` : undefined,
                      backgroundSize: "cover",
                      backgroundColor: "var(--panel2)",
                    }}
                  />
                  <div className="min-w-0">
                    <div className="font-hud text-[10px]" style={{ color: "var(--ink)" }}>
                      {figure.name}
                    </div>
                    <div className="font-hud text-[8px]" style={{ color: "var(--muted)" }}>
                      {figure.recruited ? figure.alignment : "locked"}
                    </div>
                    <p className="font-body text-[11px] line-clamp-3 mt-1" style={{ color: "var(--muted)" }}>
                      {figure.bio}
                    </p>
                  </div>
                </div>
              </button>
            ))}
        </div>
      </div>
    </div>
  );
}

export default SummonOverlay;
