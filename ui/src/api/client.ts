const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

/**
 * Narrative guidance profile for a generated turn.
 *
 * `classic` is the default and is sent by *omitting* the field: a request that
 * never mentions a mode takes exactly the path it took before this option
 * existed. `experimental` opts a single turn into the alternative
 * planner/writer guidance; it changes only narrative guidance, never the
 * engine's rules, and it does not add any model calls. `ensemble` keeps that
 * pacing profile but adds one perspective-limited actor call per present NPC.
 */
export type NarrationMode = 'classic' | 'experimental' | 'ensemble';

/** The only value worth sending; `classic` means "leave the field out". */
export const EXPERIMENTAL_NARRATION_MODE: NarrationMode = 'experimental';

export interface World {
  name: string;
  status?: string;
  updated_at?: number;
}

export interface WorldDetail {
  world_config: any;
  card_registry: any;
  canon_timeline: any;
  character_state: any;
  saves: any[];
}

export interface PlayState {
  protagonist: {
    id: string;
    name: string;
    location: string;
    power_stat: Record<string, any>;
    traits: Record<string, any>;
    knowledge_flags: string[];
    inventory: InventoryItem[];
    alive: boolean;
    relationships: Record<string, any>;
    age: string;
    capabilities?: CapabilityEvidence[];
  } | null;
  arc_progress: {
    current_checkpoint_id: string;
    current_index: number;
    total_checkpoints: number;
    completed: string[];
  };
  unlocked_cards: any[];
  story_clock: Record<string, any>;
  calendar?: { kind: string; months?: { name: string; days: number }[]; year_label?: string; era?: string } | null;
  prelude_confirmed?: boolean;
  foreshadowing_tracker: any[];
  style_card: any;
  output_length?: string;
  revision?: number;
  lifecycle_status?: string;
  story_mode?: string;
  epilogue?: { text: string; chosen_choice?: string } | null;
  active_journey?: ActiveJourney | null;
}

export interface ActiveJourney {
  journey_id: string;
  origin?: string;
  destination: string;
  remaining_route?: string[];
  elapsed_minutes?: number;
  status: 'active' | 'interrupted' | 'completed' | 'abandoned';
  reason?: string;
}

export interface TravelPreview {
  status: 'available' | 'blocked' | 'unreachable' | 'already_there' | 'unknown_destination';
  origin?: string;
  destination?: string;
  route: string[];
  legs: Array<{ from: string; to: string; travel_time_minutes: number; danger: number; tags: string[] }>;
  elapsed_minutes: number;
  estimated_ticks: number;
  narration_mode?: string;
  risk?: { level: string; known_tags: string[] };
  requirements_missing?: Array<Record<string, unknown>>;
  reason?: string;
  /**
   * The engine withheld this route's shape because it crosses unexplored
   * ground. When true, `route`/`legs` are empty on purpose and must be read as
   * "unknown", not as "no stops": rendering an empty route as a direct line
   * would invent a shortcut the engine never promised.
   */
  route_redacted?: boolean;
  /** Player-facing sentence for a redacted route, authored by the engine. */
  route_note?: string;
}

export interface InventoryItem {
  instance_id: string;
  item_id?: string | null;
  name: string;
  category: string;
  description: string;
  attributes: Record<string, any>;
  abilities: Array<Record<string, any> | string>;
  tags: string[];
  quantity: number;
  stackable?: boolean;
  custom_name?: string | null;
  condition: string;
  equipped: boolean;
  charges?: number | null;
  acquired_at_tick?: number | null;
  acquired_from?: string | null;
  item_kind?: 'consumable' | 'persistent' | 'causal_artifact' | string;
  destructibility?: 'normal' | 'protected' | 'indestructible' | string;
  drop_policy?: 'allowed' | 'bound' | string;
  usage?: { mode: 'unlimited' | 'charges' | 'quantity' | 'narrative' | string; remaining?: number };
  requirements?: Array<string | Record<string, any>>;
  state_effects?: Array<Record<string, any>>;
}

export interface CapabilityEvidence {
  capability_id: string;
  statement: string;
  proficiency?: string;
  sources?: string[];
  limits?: string[];
  tags?: string[];
}

export interface TimeSkipRequest {
  amount: number;
  unit: 'minutes' | 'hours' | 'days' | 'weeks';
  activity: string;
  interruption_policy: 'important_events' | 'known_quest_deadlines' | 'immediate_danger' | 'complete' | 'ask';
  narration_detail?: 'brief' | 'standard' | 'detailed';
  force?: boolean;
  request_id?: string;
  expected_revision?: number;
  /** Read only when the skip generates a turn; the preview ignores it. */
  narration_mode?: NarrationMode;
}

export interface TimeSkipPreview {
  requested_minutes: number;
  granted_minutes: number;
  requested_ticks: number;
  granted_ticks: number;
  start_tick: number;
  end_tick: number;
  warnings: Array<{ kind: string; event_id: string; title: string; deadline_tick: number; ticks_away: number }>;
  will_interrupt: boolean;
  stopped_reason?: string | null;
  requires_confirmation: boolean;
  activity: string;
  forced: boolean;
  blocked?: boolean;
}

export interface ChapterContinueResponse {
  chapter: {
    chapter_index: number;
    turn_index: number;
    chapter_closed: boolean;
    chapter_title: string | null;
    checkpoint_id: string;
    user_input: string;
    chapter_text: string;
    notes?: string;
    boundary_correction?: any;
    consistency_check?: any;
  };
  state_changes_applied: any;
  used_mock_llm: boolean;
  checkpoint_advanced: boolean;
  lore_rag_filter: any;
  revision?: number;
}

export interface ChapterStartRequest {
  opening_mode?: 'ai_generate' | 'user_defined';
  opening_text?: string;
  narration_mode?: NarrationMode;
}

export interface LintChapterRequest {
  chapter_index: number;
  linter_suggestions: any[];
}

export interface RuntimeConfigStatus {
  llm_provider: string;
  model_name?: string;
  temperature?: number;
  base_url?: string;
  api_key_masked?: string | null;
  api_key_source?: string;
  has_api_key?: boolean;
  model?: string;
}

export interface RuntimeConfigUpdatePayload {
  llm_provider?: string;
  model_name?: string;
  temperature?: number;
  base_url?: string;
  api_key?: string;
  api_key_action?: 'keep' | 'replace' | 'delete';
}

async function fetchJSON<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, options);
  if (!res.ok) {
    const text = await res.text();
    let message = `API error ${res.status}: ${text}`;
    let detail: any;
    try {
      const body = JSON.parse(text);
      detail = body?.detail;
      if (typeof detail === 'string') {
        message = detail;
      } else if (detail && typeof detail === 'object') {
        message = detail.message || detail.reason || `API error ${res.status}`;
      }
    } catch {
      // keep the raw text message
    }
    const error = new Error(message) as Error & { detail?: any };
    if (detail !== undefined) {
      error.detail = detail;
    }
    throw error;
  }
  return res.json();
}

export const api = {
  worlds: {
    list: () => fetchJSON<{ worlds: World[] }>('/worlds').then(r => r.worlds),
    get: (name: string) => fetchJSON<WorldDetail>(`/worlds/${name}`),
    create: (name: string, payload?: { prompt?: string; scope_type?: string; interaction_mode?: string }) =>
      fetchJSON<any>(`/worlds/${name}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {}),
      }),
    delete: (name: string) =>
      fetchJSON<any>(`/worlds/${name}`, { method: 'DELETE' }),
    updateConfig: (name: string, payload: Record<string, any>) =>
      fetchJSON<any>(`/worlds/${name}/world_config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }),
    seedDemo: (name: string, overwrite: boolean = false) =>
      fetchJSON<any>(`/worlds/${name}/seed-demo?overwrite=${overwrite}`, { method: 'POST' }),
    affinityGraph: (name: string) =>
      fetchJSON<{ nodes: any[]; edges: any[] }>(`/worlds/${name}/affinity-graph`),
    foreshadowings: {
      get: (name: string) => fetchJSON<any[]>(`/worlds/${name}/foreshadowings`),
      add: (name: string, payload: any) =>
        fetchJSON<any>(`/worlds/${name}/foreshadowings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }),
    },
  },
  play: {
    state: (worldName: string) =>
      fetchJSON<PlayState>(`/worlds/${worldName}/play-state`),
    start: (worldName: string, req: ChapterStartRequest = {}) =>
      fetchJSON<any>(`/worlds/${worldName}/chapter/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      }),
    continue: (worldName: string, userInput: string, opts?: { requestId?: string; expectedRevision?: number; narrationMode?: NarrationMode }) =>
      fetchJSON<ChapterContinueResponse>(`/worlds/${worldName}/chapter/continue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_input: userInput,
          request_id: opts?.requestId,
          expected_revision: opts?.expectedRevision,
          // Left undefined in classic mode, so the default request body is
          // unchanged from before this option existed.
          narration_mode: opts?.narrationMode,
        }),
      }),
    previewTimeSkip: (worldName: string, req: TimeSkipRequest) =>
      fetchJSON<TimeSkipPreview>(`/worlds/${worldName}/time-skip/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req),
      }),
    executeTimeSkip: (worldName: string, req: TimeSkipRequest) =>
      fetchJSON<ChapterContinueResponse>(`/worlds/${worldName}/time-skip/execute`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req),
      }),
    regenerate: (worldName: string, opts?: { requestId?: string; expectedRevision?: number; narrationMode?: NarrationMode; userInput?: string }) =>
      fetchJSON<any>(`/worlds/${worldName}/chapter/regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: opts?.requestId,
          expected_revision: opts?.expectedRevision,
          narration_mode: opts?.narrationMode,
          user_input: opts?.userInput,
        }),
      }),
    endgameStatus: (worldName: string) =>
      fetchJSON<any>(`/worlds/${worldName}/chapter/endgame-status`),
    epilogueChoices: (worldName: string) =>
      fetchJSON<{ choices: string[] }>(`/worlds/${worldName}/chapter/generate-epilogue-choices`, { method: 'POST' }),
    generateEpilogue: (worldName: string, chosenChoice: string) =>
      fetchJSON<any>(`/worlds/${worldName}/chapter/generate-epilogue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chosen_choice: chosenChoice }),
      }),
    getChapters: (worldName: string) =>
      fetchJSON<any[]>(`/worlds/${worldName}/chapters`),
    prelude: {
      generate: (worldName: string) =>
        fetchJSON<any>(`/worlds/${worldName}/chapter/generate-prelude`, { method: 'POST' }),
      regenerate: (worldName: string) =>
        fetchJSON<any>(`/worlds/${worldName}/chapter/regenerate-prelude`, { method: 'POST' }),
      confirm: (worldName: string) =>
        fetchJSON<any>(`/worlds/${worldName}/chapter/confirm-prelude`, { method: 'POST' }),
    },
    lint: (worldName: string, req: LintChapterRequest) =>
      fetchJSON<any>(`/worlds/${worldName}/lint-chapter`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      }),
    rewrite: (worldName: string, chapterIndex: number, linterSuggestions: any[]) =>
      fetchJSON<any>(`/worlds/${worldName}/chapter/rewrite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chapter_index: chapterIndex, linter_suggestions: linterSuggestions }),
      }),
  },
  builder: {
    interview: (prompt: string, scope_type: string = 'arc-only') =>
      fetchJSON<{ questions: string[] }>('/builder/interview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, scope_type }),
      }),
    interviewRespond: (prompt: string, answers: string[], scope_type: string = 'arc-only') =>
      fetchJSON<{ refined_prompt: string; scope_type?: string }>('/builder/interview/respond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, answers, scope_type }),
      }),
    confirmCheckpoints: (worldName: string) =>
      fetchJSON<{ status: string }>(`/worlds/${worldName}/builder/confirm-checkpoints`, {
        method: 'POST',
      }),
    step: (worldName: string) =>
      fetchJSON<{ status: string; message?: string; fixed_cards?: any; sanitized_conditions?: any }>(`/worlds/${worldName}/builder/step`, {
        method: 'POST',
      }),
    status: (worldName: string) =>
      fetchJSON<{ creation_status: string; next_step: string | null; steps_done: string[]; has_checkpoints: boolean; has_cards: boolean; has_characters: boolean; has_events: boolean }>(`/worlds/${worldName}/builder/status`),
    events: (worldName: string) =>
      fetchJSON<{ status: string; events_created: number }>(`/worlds/${worldName}/builder/events`, { method: 'POST' }),
    replan: (worldName: string, concept: string) =>
      fetchJSON<{ regenerate_steps: string[]; preserved_steps: string[]; note: string }>(`/worlds/${worldName}/builder/replan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ concept }),
      }),
    applyReplan: (worldName: string) =>
      fetchJSON<{ applied: boolean; regenerate_steps: string[]; preserved_steps: string[]; creation_status: string }>(`/worlds/${worldName}/builder/apply-replan`, { method: 'POST' }),
  },
  creator: {
    saves: {
      list: async (worldName: string) => {
        const res = await fetchJSON<{ saves?: any[] } | any[]>(`/worlds/${worldName}/saves`);
        if (Array.isArray(res)) return res;
        return res?.saves || [];
      },
      create: (worldName: string, label: string) =>
        fetchJSON<any>(`/worlds/${worldName}/saves`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label }),
        }),
      restore: (worldName: string, saveId: string) =>
        fetchJSON<any>(`/worlds/${worldName}/saves/${saveId}/restore`, {
          method: 'POST',
        }),
      branch: (worldName: string, saveId: string, newWorldName: string) =>
        fetchJSON<any>(`/worlds/${worldName}/saves/${saveId}/branch`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ new_world_name: newWorldName }),
        }),
    },
    styleCard: {
      get: (worldName: string) => fetchJSON<any>(`/worlds/${worldName}/style-card`),
      update: (worldName: string, cardData: any) =>
        fetchJSON<any>(`/worlds/${worldName}/style-card`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(cardData),
        }),
    },
    traits: {
      get: (worldName: string) => fetchJSON<any>(`/worlds/${worldName}/traits`),
      update: (worldName: string, traits: any) =>
        fetchJSON<any>(`/worlds/${worldName}/traits`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(traits),
        }),
    },
    canonLog: {
      get: async (worldName: string) => {
        const res = await fetchJSON<{ world?: string; facts?: any[]; total?: number } | any[]>(`/worlds/${worldName}/studio/canon-log`);
        if (Array.isArray(res)) return res;
        return res?.facts || [];
      },
      add: (worldName: string, entry: any) =>
        fetchJSON<any>(`/worlds/${worldName}/studio/canon-log`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(entry),
        }),
    },
    export: (worldName: string) => fetchJSON<any>(`/worlds/${worldName}/export`),
    fork: (worldName: string, checkpointId: string, newWorldName: string) =>
      fetchJSON<any>(`/worlds/${worldName}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ checkpoint_id: checkpointId, new_world_name: newWorldName }),
      }),
  },
  config: {
    get: () => fetchJSON<RuntimeConfigStatus>('/runtime-config'),
    update: (config: RuntimeConfigUpdatePayload) =>
      fetchJSON<RuntimeConfigStatus>('/runtime-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      }),
    clearApiKey: () =>
      fetchJSON<RuntimeConfigStatus>('/runtime-config/api-key', { method: 'DELETE' }),
    testConnection: () =>
      fetchJSON<{ ok?: boolean; status?: string; message: string }>('/runtime-config/test-connection', { method: 'POST' }),
  },
  codex: {
    get: (worldName: string) =>
      fetchJSON<any>(`/worlds/${worldName}/codex`),
  },
  discovery: {
    questBoard: (worldName: string) =>
      fetchJSON<{ quests: any[]; enabled: boolean }>(`/worlds/${worldName}/quest_board`),
    journal: (worldName: string) =>
      fetchJSON<{ entries: any[] }>(`/worlds/${worldName}/journal`),
  },
  map: {
    get: (worldName: string) =>
      fetchJSON<{ locations: any[] }>(`/worlds/${worldName}/location-map`),
    status: (worldName: string) =>
      fetchJSON<{ locations: any[] }>(`/worlds/${worldName}/location-map/status`),
    previewTravel: (worldName: string, destination: string) =>
      fetchJSON<TravelPreview>(`/worlds/${worldName}/travel/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ destination }),
      }),
  },
};
