import { useQuery } from '@tanstack/react-query'
import type {
  AgentOut,
  AgentTurns,
  ErrorKind,
  HarnessNote,
  RunErrors,
  RunDetail,
  RunEntries,
  RunSummary,
  SearchHit,
  SourceEventOut,
  TurnOut,
} from './types'

type Params = Record<
  string,
  string | number | boolean | null | undefined | readonly (string | number)[]
>
type ErrorParams = Omit<Params, 'kind'> & { kind?: ErrorKind[] }

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`)
    this.status = status
    this.detail = detail
  }
}

export async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`/api/dev${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new ApiError(response.status, await response.text())
  return response.json() as Promise<T>
}

async function get<T>(path: string, params?: Params): Promise<T> {
  const url = new URL(`/api/dev${path}`, window.location.origin)
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      for (const item of value) url.searchParams.append(key, String(item))
    } else {
      url.searchParams.set(key, String(value))
    }
  }
  const response = await fetch(url, { headers: { accept: 'application/json' } })
  if (!response.ok) {
    const body = await response.text()
    throw new ApiError(response.status, body.slice(0, 400))
  }
  return response.json() as Promise<T>
}

const LIVE = 2000

export function useRuns() {
  return useQuery({
    queryKey: ['runs'],
    queryFn: () => get<RunSummary[]>('/runs'),
    refetchInterval: (query) =>
      query.state.data?.some((run) => run.state === 'running') ? LIVE : false,
  })
}

export function useRun(runId: number | null) {
  return useQuery({
    queryKey: ['run', runId],
    queryFn: () => get<RunDetail>(`/runs/${runId}`),
    refetchInterval: LIVE,
    enabled: runId !== null,
  })
}

export function useRunAgents(runId: number | null, live: boolean) {
  return useQuery({
    queryKey: ['run-agents', runId],
    queryFn: () => get<AgentOut[]>(`/runs/${runId}/agents`),
    enabled: runId !== null,
    refetchInterval: live ? LIVE : false,
  })
}

export function useRunEntries(runId: number | null) {
  return useQuery({
    queryKey: ['run-entries', runId],
    queryFn: () => get<RunEntries>(`/runs/${runId}/entries`),
    enabled: runId !== null,
  })
}

export function useNotes(runId: number | null) {
  return useQuery({
    queryKey: ['harness-notes', runId],
    queryFn: () => get<HarnessNote[]>('/notes', runId === null ? {} : { run_id: runId }),
    refetchInterval: LIVE,
  })
}

export function useRunErrors(runId: number | null, filters: ErrorParams) {
  return useQuery({
    queryKey: ['run-errors', runId, filters],
    queryFn: () => get<RunErrors>(`/runs/${runId}/errors`, filters),
    enabled: runId !== null,
  })
}

export function useRunTurns(runId: number | null, live: boolean, enabled: boolean) {
  return useQuery({
    queryKey: ['run-turns', runId],
    queryFn: () => get<TurnOut[]>(`/runs/${runId}/turns`),
    enabled: enabled && runId !== null,
    refetchInterval: live ? LIVE : false,
  })
}

export function useSourceEvents(runId: number | null) {
  return useQuery({
    queryKey: ['source-events', runId],
    queryFn: () => get<SourceEventOut[]>(`/runs/${runId}/source-events`),
    enabled: runId !== null,
  })
}

export function useAgents(filters: Params, enabled: boolean) {
  return useQuery({
    queryKey: ['agents', filters],
    queryFn: () => get<AgentOut[]>('/agents', filters),
    enabled,
  })
}

export function useAgentTurns(agentId: number | null, live: boolean) {
  return useQuery({
    queryKey: ['turns', agentId],
    queryFn: () => get<AgentTurns>(`/agents/${agentId}/turns`),
    enabled: agentId !== null,
    refetchInterval: live ? LIVE : false,
  })
}

export function useTranscriptSearch(q: string, runId: number | null) {
  return useQuery({
    queryKey: ['search', q, runId],
    queryFn: () => get<SearchHit[]>('/search', { q, run_id: runId }),
    enabled: q.trim().length > 1,
  })
}
