export type Role = 'orchestrator' | 'assistant' | 'researcher' | 'refuter'
export type AgentState = 'running' | 'idle' | 'done' | 'failed'
export type RunState = 'running' | 'finished' | 'exhausted' | 'failed'
export type TurnKind = 'system' | 'user' | 'assistant' | 'tool_call' | 'tool_result'
export type EntryState = 'banked' | 'parked' | 'noise' | 'insufficient'
export type SignalClass = 'weak' | 'strong' | 'noise'
export type PatternKind = 'faintness' | 'substance' | 'delivery'
export type ErrorKind = 'source' | 'agent' | 'model' | 'tool' | 'refutation' | 'run' | 'fetch' | 'citation'

export interface Counts {
  agents: number
  entries: number
  turns: number
  documents: number
  citations: number
  cost_usd: number
}

export interface RunSummary {
  id: number
  query: string
  direction_ru: string | null
  direction_en: string | null
  state: RunState
  started_at: string
  finished_at: string | null
  heartbeat_at: string | null
  is_live: boolean
  elapsed_s: number
  counts: Counts
}

export interface RunDetail {
  run: Omit<RunSummary, 'elapsed_s' | 'counts'>
  meta: {
    accounting: Record<string, unknown>
    agents: Record<string, unknown>[]
    context_sources: Record<string, unknown>[]
    per_source: Record<string, unknown>[]
    field_counts: [number, number]
    unbacked_patterns: number
    models_used: Record<string, string>
  }
}

export interface AgentMetrics {
  turns: number
  model_calls: number
  model_call_budget: number | null
  cost_usd: number
  prompt_tokens: number
  completion_tokens: number
  cached_tokens: number
  wall_time_s: number
  error_turns: number
}

export interface AgentOut {
  id: number
  parent_agent_id: number | null
  role: Role
  model: string
  field: string | null
  brief: string | null
  state: AgentState
  created_at: string
  last_called_at: string | null
  returned_at: string | null
  metrics: AgentMetrics
  run_id: number | null
  query: string | null
  is_live: boolean
}

export interface TurnOut {
  id: number
  agent_id: number
  seq: number
  kind: TurnKind
  content: TurnContent
  prompt_tokens: number | null
  completion_tokens: number | null
  cached_tokens: number | null
  cache_write_tokens: number | null
  reasoning_tokens: number | null
  cost_usd: number | null
  duration_ms: number | null
  attempts: number | null
  provider: string | null
  generation_id: string | null
  created_at: string
}

export interface ProviderMessage {
  role?: string
  content?: string | null
  reasoning?: string | null
  reasoning_details?: unknown[] | null
  refusal?: string | null
  tool_calls?: { id?: string; function?: { name?: string; arguments?: string } }[] | null
}

export interface TurnContent {
  content?: string
  model?: string
  prompt_sha256?: string
  message?: ProviderMessage
  error?: string
  reason?: string
  id?: string
  name?: string
  arguments?: Record<string, unknown>
  payload?: Record<string, unknown>
}

export interface AgentTurns {
  agent: AgentOut
  turns: TurnOut[]
}

export interface DocumentOut {
  id: number
  url: string
  title: string | null
  source_name: string
  source_type: string
  source_lang: string
  source_tier: number
  published_at: string | null
  adapter: string | null
  retrieved: boolean
}

export interface CitationOut {
  id: number
  entry_id: number
  document_id: number
  quote: string
  summary_ru: string | null
  title_original: string | null
  verified: boolean
  verified_at: string | null
  document: DocumentOut
}

export interface EntryPatternOut {
  id: number
  entry_id: number
  kind: PatternKind
  pattern: string
  strength: number | null
  citation_id: number | null
}

export interface EntryOut {
  id: number
  run_id: number
  field_id: number | null
  researched_by_agent_id: number | null
  written_by_agent_id: number | null
  technology_id: number | null
  name_ru: string
  name_en: string | null
  transition_ru: string
  state: EntryState
  score: number
  weak_score: number | null
  signal_class: SignalClass | null
  substance: number | null
  momentum: number | null
  faintness: number | null
  why_ru: string
  current_state_ru: string | null
  dynamics_ru: string | null
  what_would_refute_ru: string | null
  searches_run: number
  sources_checked: number
  problem_ru: string | null
  advantage_ru: string | null
  case_example_ru: string | null
  refuter_agent_id: number | null
  rebutted: boolean
  created_at: string
  citations: CitationOut[]
  entry_patterns: EntryPatternOut[]
}

export interface RefutationOut {
  id: number
  run_id: number
  researcher_agent_id: number
  refuter_agent_id: number
  rebuttal_required: boolean
  state: string
  outcome: Record<string, unknown> | null
  collected: boolean
  delivery_version: number
  created_at: string
  completed_at: string | null
}

export interface RunEntries {
  entries: EntryOut[]
  refutations: RefutationOut[]
}

export interface SourceEventOut {
  id: number
  ts: string
  adapter: string
  host: string | null
  query: string | null
  via: string | null
  ok: boolean
  items: number | null
  error: string | null
  duration_ms: number | null
  bytes: number | null
  requests: number | null
  cached: boolean
  agent_id: number | null
}

export interface RunError {
  id: string
  kind: ErrorKind
  created_at: string
  agent_id: number | null
  role: Role | null
  title: string
  message: string
  turn_id: number | null
  turn_seq: number | null
  source_event_id: number | null
  refutation_id: number | null
  run_event_id: number | null
  document_id: number | null
  citation_id: number | null
  entry_id: number | null
}

export interface HarnessNote {
  id: number
  run_id: number | null
  agent_id: number | null
  role: Role | null
  model: string | null
  kind: 'bug' | 'friction' | 'missing' | 'idea'
  tool: string | null
  text: string
  meta: { last_call?: { name?: string; arguments?: string; result?: string } | null }
  created_at: string
}

export interface RunErrors {
  errors: RunError[]
  counts: Record<ErrorKind, number>
  total: number
}

export interface SearchHit {
  id: number
  seq: number
  kind: TurnKind
  agent_id: number
  role: Role
  run_id: number
  created_at: string
  snippet: string
}
