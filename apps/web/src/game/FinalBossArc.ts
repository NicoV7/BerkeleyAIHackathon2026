import { api } from "../api/client";

export interface PlaythroughProfile {
  style: Record<string, number>;
  alignment: Record<string, number>;
  weakness: string[];
  recent_quotes: string[];
  bosses_defeated: number;
  dungeons_cleared: number;
  figures_recruited: string[];
}

export interface ProfileResponse {
  profile: PlaythroughProfile;
  boss_prompt_blurbs: {
    style: string;
    alignment: string;
    weakness: string;
  };
}

export interface FinalBossTriggerOptions {
  requiredDungeons?: number;
  finalRegionEntered?: boolean;
}

export async function fetchPlaythroughProfile(runId: string): Promise<ProfileResponse> {
  return api.get<ProfileResponse>(`/api/runs/${runId}/profile`);
}

export function shouldTriggerFinalBoss(
  profile: PlaythroughProfile,
  options: FinalBossTriggerOptions = {}
): boolean {
  const requiredDungeons = options.requiredDungeons ?? 3;
  return profile.dungeons_cleared >= requiredDungeons && options.finalRegionEntered === true;
}

export function buildFinalBossPrompt(profileResponse: ProfileResponse): string {
  const { profile, boss_prompt_blurbs: blurbs } = profileResponse;
  const quotes = profile.recent_quotes.slice(0, 3).join(" | ");
  return [
    "You are the final judge of this debate RPG playthrough.",
    blurbs.style,
    blurbs.alignment,
    blurbs.weakness,
    quotes ? `Mirror these player tells: ${quotes}` : "The player has left few quoted tells.",
    "Reveal in three phases: mirror, judge, verdict.",
  ].join("\n");
}

export async function recordWorldEvent(
  runId: string,
  kind: string,
  data: Record<string, unknown> = {}
): Promise<{ event: unknown; completed_quests: string[] }> {
  return api.post(`/api/runs/${runId}/events`, { kind, data });
}
