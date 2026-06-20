/**
 * GambitEditor — author / reorder / enable gambit rules for a monster.
 *
 * Usage:
 *   <GambitEditor monsterId="..." />
 *
 * Fetches rules from GET /api/monsters/{id}/gambits
 * Saves full list via PUT /api/monsters/{id}/gambits
 *
 * Condition kinds: self_hp_pct, ally_hp_pct, enemy_hp_pct, last_verdict_score,
 *                  turn_no, topic_keyword, momentum
 * Ops: <, <=, >, >=, ==, contains
 * Action kinds: use_skill (+ skill_id), target (+ who), tone (+ value), default
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface GambitRuleModel {
  id?: string | null;
  priority: number;
  condition: Record<string, unknown>;
  action: Record<string, unknown>;
  enabled: boolean;
}

export interface GambitList {
  monster_id: string;
  rules: GambitRuleModel[];
}

// ---------------------------------------------------------------------------
// Constants for dropdowns
// ---------------------------------------------------------------------------

const CONDITION_KINDS = [
  "self_hp_pct",
  "ally_hp_pct",
  "enemy_hp_pct",
  "last_verdict_score",
  "turn_no",
  "topic_keyword",
  "momentum",
] as const;

const OPS = ["<", "<=", ">", ">=", "==", "contains"] as const;

const ACTION_KINDS = ["default", "use_skill", "target", "tone"] as const;

const TARGET_WHO = ["lowest_hp_enemy", "highest_hp_enemy"] as const;

// ---------------------------------------------------------------------------
// Blank rule factory
// ---------------------------------------------------------------------------

let _nextPriority = 0;

function blankRule(): GambitRuleModel {
  return {
    priority: _nextPriority++,
    condition: { kind: "self_hp_pct", op: "<", value: 50 },
    action: { kind: "default" },
    enabled: true,
  };
}

// ---------------------------------------------------------------------------
// Condition editor
// ---------------------------------------------------------------------------

function ConditionEditor({
  condition,
  onChange,
}: {
  condition: Record<string, unknown>;
  onChange: (c: Record<string, unknown>) => void;
}) {
  const kind = (condition.kind as string) ?? "self_hp_pct";
  const op = (condition.op as string) ?? "<";
  const value = condition.value ?? "";

  function set(patch: Record<string, unknown>) {
    onChange({ ...condition, ...patch });
  }

  return (
    <div className="flex items-center gap-1 flex-wrap text-xs">
      <span className="font-hud" style={{ color: "var(--muted)" }}>IF</span>
      <select
        className="pixel-field text-xs"
        value={kind}
        onChange={(e) => {
          // Reset op when switching to topic_keyword
          const newKind = e.target.value;
          const newOp = newKind === "topic_keyword" ? "contains" : "<";
          set({ kind: newKind, op: newOp, value: newKind === "topic_keyword" ? "" : 50 });
        }}
      >
        {CONDITION_KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </select>

      {kind !== "topic_keyword" && (
        <select
          className="pixel-field text-xs"
          value={op}
          onChange={(e) => set({ op: e.target.value })}
        >
          {OPS.filter((o) => o !== "contains").map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      )}

      {kind === "topic_keyword" ? (
        <input
          className="pixel-field text-xs w-28"
          placeholder="keyword"
          value={String(value)}
          onChange={(e) => set({ op: "contains", value: e.target.value })}
        />
      ) : (
        <input
          className="pixel-field text-xs w-16"
          type="number"
          value={Number(value)}
          onChange={(e) => set({ value: Number(e.target.value) })}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Action editor
// ---------------------------------------------------------------------------

function ActionEditor({
  action,
  onChange,
}: {
  action: Record<string, unknown>;
  onChange: (a: Record<string, unknown>) => void;
}) {
  const kind = (action.kind as string) ?? "default";

  function set(patch: Record<string, unknown>) {
    onChange({ ...action, ...patch });
  }

  return (
    <div className="flex items-center gap-1 flex-wrap text-xs">
      <span className="font-hud" style={{ color: "var(--muted)" }}>THEN</span>
      <select
        className="pixel-field text-xs"
        value={kind}
        onChange={(e) => onChange({ kind: e.target.value })}
      >
        {ACTION_KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </select>

      {kind === "use_skill" && (
        <input
          className="pixel-field text-xs w-32"
          placeholder="skill_id"
          value={String(action.skill_id ?? "")}
          onChange={(e) => set({ skill_id: e.target.value })}
        />
      )}

      {kind === "target" && (() => {
        const who = String(action.who ?? "lowest_hp_enemy");
        const isPreset = (TARGET_WHO as readonly string[]).includes(who);
        return (
          <>
            <select
              className="pixel-field text-xs"
              value={isPreset ? who : "__custom__"}
              onChange={(e) =>
                set({ who: e.target.value === "__custom__" ? "" : e.target.value })
              }
            >
              {TARGET_WHO.map((w) => (
                <option key={w} value={w}>
                  {w}
                </option>
              ))}
              <option value="__custom__">specific id…</option>
            </select>
            {!isPreset && (
              <input
                className="pixel-field text-xs w-32"
                placeholder="monster id"
                value={who}
                onChange={(e) => set({ who: e.target.value })}
              />
            )}
          </>
        );
      })()}

      {kind === "tone" && (
        <input
          className="pixel-field text-xs w-28"
          placeholder="e.g. aggressive"
          value={String(action.value ?? "")}
          onChange={(e) => set({ value: e.target.value })}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rule row
// ---------------------------------------------------------------------------

function RuleRow({
  rule,
  index,
  total,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  rule: GambitRuleModel;
  index: number;
  total: number;
  onChange: (r: GambitRuleModel) => void;
  onRemove: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
}) {
  return (
    <div
      className={`pixel-inset p-2 space-y-1.5 ${rule.enabled ? "" : "opacity-50"}`}
    >
      <div className="flex items-center gap-2">
        {/* Priority badge */}
        <span className="text-xs w-6 text-center" style={{ color: "var(--muted)" }}>
          {index + 1}
        </span>

        {/* Enable toggle */}
        <button
          className={`pixel-btn text-[10px] ${rule.enabled ? "pixel-btn--accent" : ""}`}
          style={{ padding: "2px 6px" }}
          onClick={() => onChange({ ...rule, enabled: !rule.enabled })}
        >
          {rule.enabled ? "on" : "off"}
        </button>

        {/* Reorder */}
        <button
          disabled={index === 0}
          onClick={onMoveUp}
          className="pixel-btn text-[10px]"
          style={{ padding: "2px 6px" }}
        >
          ↑
        </button>
        <button
          disabled={index === total - 1}
          onClick={onMoveDown}
          className="pixel-btn text-[10px]"
          style={{ padding: "2px 6px" }}
        >
          ↓
        </button>

        {/* Remove */}
        <button
          onClick={onRemove}
          className="pixel-btn pixel-btn--enemy ml-auto text-[10px]"
          style={{ padding: "2px 6px" }}
        >
          ✕
        </button>
      </div>

      <ConditionEditor
        condition={rule.condition}
        onChange={(c) => onChange({ ...rule, condition: c })}
      />
      <ActionEditor
        action={rule.action}
        onChange={(a) => onChange({ ...rule, action: a })}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main editor
// ---------------------------------------------------------------------------

export function GambitEditor({ monsterId }: { monsterId: string }) {
  const [rules, setRules] = useState<GambitRuleModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const fetchRules = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await api.get<GambitList>(`/api/monsters/${monsterId}/gambits`);
      // Seed _nextPriority above current max
      const maxP = list.rules.reduce((m, r) => Math.max(m, r.priority), -1);
      _nextPriority = maxP + 1;
      setRules(list.rules);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [monsterId]);

  useEffect(() => {
    void fetchRules();
  }, [fetchRules]);

  function updateRule(index: number, updated: GambitRuleModel) {
    setRules((prev) => prev.map((r, i) => (i === index ? updated : r)));
  }

  function removeRule(index: number) {
    setRules((prev) => prev.filter((_, i) => i !== index));
  }

  function addRule() {
    setRules((prev) => [...prev, blankRule()]);
  }

  function move(index: number, dir: -1 | 1) {
    setRules((prev) => {
      const next = [...prev];
      const target = index + dir;
      if (target < 0 || target >= next.length) return prev;
      [next[index], next[target]] = [next[target], next[index]];
      // Reassign priorities to match display order
      return next.map((r, i) => ({ ...r, priority: i }));
    });
  }

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const normalized = rules.map((r, i) => ({ ...r, priority: i }));
      await api.put<GambitList>(`/api/monsters/${monsterId}/gambits`, {
        monster_id: monsterId,
        rules: normalized,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="p-4 text-sm text-white/40">Loading gambits for {monsterId}…</div>
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between px-3 py-2"
        style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}
      >
        <div className="font-hud text-sm">Gambits</div>
        <div className="flex items-center gap-2">
          {error && (
            <span className="font-hud text-[10px]" style={{ color: "var(--danger)" }}>
              {error}
            </span>
          )}
          {saved && (
            <span className="font-hud text-[10px]" style={{ color: "var(--win)" }}>
              Saved!
            </span>
          )}
          <button onClick={addRule} className="pixel-btn text-[10px]">
            + Add Rule
          </button>
          <button
            onClick={save}
            disabled={saving}
            className="pixel-btn pixel-btn--accent text-[10px]"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {/* Rule list */}
      <div className="flex-1 overflow-y-auto p-2 space-y-2">
        {rules.length === 0 && (
          <div className="text-sm text-white/30 italic text-center py-6">
            No rules yet. Add one to get started.
          </div>
        )}
        {rules.map((rule, i) => (
          <RuleRow
            key={rule.id ?? `rule-${i}`}
            rule={rule}
            index={i}
            total={rules.length}
            onChange={(updated) => updateRule(i, updated)}
            onRemove={() => removeRule(i)}
            onMoveUp={() => move(i, -1)}
            onMoveDown={() => move(i, 1)}
          />
        ))}
      </div>
    </div>
  );
}

export default GambitEditor;
