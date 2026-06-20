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
      <span className="text-white/40">IF</span>
      <select
        className="bg-white/5 border border-white/10 rounded px-1 py-0.5"
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
          className="bg-white/5 border border-white/10 rounded px-1 py-0.5"
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
          className="bg-white/5 border border-white/10 rounded px-1 py-0.5 w-28"
          placeholder="keyword"
          value={String(value)}
          onChange={(e) => set({ op: "contains", value: e.target.value })}
        />
      ) : (
        <input
          className="bg-white/5 border border-white/10 rounded px-1 py-0.5 w-16"
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
      <span className="text-white/40">THEN</span>
      <select
        className="bg-white/5 border border-white/10 rounded px-1 py-0.5"
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
          className="bg-white/5 border border-white/10 rounded px-1 py-0.5 w-32"
          placeholder="skill_id"
          value={String(action.skill_id ?? "")}
          onChange={(e) => set({ skill_id: e.target.value })}
        />
      )}

      {kind === "target" && (
        <select
          className="bg-white/5 border border-white/10 rounded px-1 py-0.5"
          value={String(action.who ?? "lowest_hp_enemy")}
          onChange={(e) => set({ who: e.target.value })}
        >
          {TARGET_WHO.map((w) => (
            <option key={w} value={w}>
              {w}
            </option>
          ))}
          <option value="__custom__">specific id…</option>
        </select>
      )}

      {kind === "tone" && (
        <input
          className="bg-white/5 border border-white/10 rounded px-1 py-0.5 w-28"
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
      className={`border rounded p-2 space-y-1.5 ${
        rule.enabled ? "border-white/15" : "border-white/5 opacity-50"
      }`}
    >
      <div className="flex items-center gap-2">
        {/* Priority badge */}
        <span className="text-xs text-white/30 w-6 text-center">{index + 1}</span>

        {/* Enable toggle */}
        <button
          className={`text-xs px-1.5 py-0.5 rounded ${
            rule.enabled ? "bg-indigo-700" : "bg-white/10"
          }`}
          onClick={() => onChange({ ...rule, enabled: !rule.enabled })}
        >
          {rule.enabled ? "on" : "off"}
        </button>

        {/* Reorder */}
        <button
          disabled={index === 0}
          onClick={onMoveUp}
          className="text-xs px-1.5 py-0.5 rounded bg-white/5 hover:bg-white/10 disabled:opacity-20"
        >
          ↑
        </button>
        <button
          disabled={index === total - 1}
          onClick={onMoveDown}
          className="text-xs px-1.5 py-0.5 rounded bg-white/5 hover:bg-white/10 disabled:opacity-20"
        >
          ↓
        </button>

        {/* Remove */}
        <button
          onClick={onRemove}
          className="ml-auto text-xs px-1.5 py-0.5 rounded bg-rose-900/50 hover:bg-rose-800/70"
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
      <div className="flex items-center justify-between px-3 py-2 border-b border-white/10">
        <div className="text-sm font-semibold">Gambits</div>
        <div className="flex items-center gap-2">
          {error && <span className="text-xs text-red-400">{error}</span>}
          {saved && <span className="text-xs text-green-400">Saved!</span>}
          <button
            onClick={addRule}
            className="text-xs px-2 py-1 rounded bg-white/10 hover:bg-white/20"
          >
            + Add Rule
          </button>
          <button
            onClick={save}
            disabled={saving}
            className="text-xs px-2 py-1 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40"
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
