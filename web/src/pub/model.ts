import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

export type SignalClass = 'weak' | 'strong' | 'noise'
export type Verdict = 'proven' | 'partly' | 'not_proven'

export interface Citation {
  url: string
  quote: string
  verified: boolean
  name: string
  published_at: string | null
  type: string
  lang: string
  tier: string
  title_original?: string | null
  summary_ru?: string | null
  summary_is_generated?: boolean
}

export interface Pattern {
  kind: string
  pattern: string
  citation_index: number
  strength: number | null
}

export interface RefutationClaim {
  verdict: Verdict | string
  claim?: string | null
  opposite?: string | null
  effect?: string | null
  url?: string | null
  quote?: string | null
}

export interface RefutationResponse {
  attack?: string | null
  response?: string | null
  changes?: string | null
}

export interface Refutation {
  state: string
  rebutted: boolean
  claims: RefutationClaim[]
  responses: RefutationResponse[]
  queries_and_venues?: string | null
}

export interface Entry {
  id: number
  rank: number
  name_ru: string
  name_en: string | null
  edits?: { created_at: string; reason: string; changed: string[] }[]
  forced_reason?: string | null
  transition_ru: string
  score: number
  weak_score: number | null
  signal_class: SignalClass | null
  substance: number | null
  momentum: number | null
  faintness: number | null
  state: string
  why_ru: string
  current_state_ru: string | null
  dynamics_ru: string | null
  what_would_refute_ru: string | null
  searches_run: number
  sources_checked: number
  problem_ru: string | null
  advantage_ru: string | null
  case_example_ru: string | null
  patterns: Pattern[]
  citations: Citation[]
  refutation?: Refutation | null
}

export interface Stats {
  candidates_found: number
  sources_processed: number
  high_confidence_count: number
  confidence_threshold: number
  signals_count: number
  agents_spawned: number
  citations_verified: number
  citations_total: number
  elapsed_seconds: number
}

export interface Report {
  run_id: number
  state: string
  query: string
  generated_at: string
  stats: Stats
  entries: Entry[]
  top_n?: number | null
  models_used: Record<string, string>
}

export interface RunRow {
  id: number
  query: string
  direction_ru: string | null
  state: string
  started_at: string
  finished_at: string | null
  elapsed_s: number
  is_live: boolean
  counts: { entries: number; citations: number; citations_verified?: number; documents: number; agents: number; cost_usd: number }
}

export interface Month {
  m: string
  n: number
  total: number
}

export interface EntrySeries {
  entry_id?: number
  entry_rank?: number
  term?: string
  q?: string
  months: Month[]
  source?: string
  too_broad?: boolean
  matched?: number
  matched_total?: number
  elapsed_ms?: number
}

export const findEntry = (entries: Entry[], ref: number) =>
  ref ? entries.find((e) => e.id === ref) ?? entries.find((e) => e.rank === ref) : undefined

export const seriesForEntry = (series: EntrySeries[] | undefined, e: Entry) =>
  series?.find((x) => (x.entry_id != null ? x.entry_id === e.id : x.entry_rank === e.rank))

export interface RunEvent {
  id: number
  kind: string
  ts?: string
  [key: string]: unknown
}

export class HttpError extends Error {
  status: number
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
  }
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { accept: 'application/json' } })
  if (!r.ok) {
    let detail = r.statusText
    try {
      const body = await r.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      detail = (await r.text().catch(() => '')) || detail
    }
    throw new HttpError(r.status, detail)
  }
  return r.json() as Promise<T>
}

export function useRuns() {
  return useQuery({
    queryKey: ['pub-runs'],
    queryFn: async () => {
      try {
        return await getJson<RunRow[]>('/api/runs')
      } catch (e) {
        if (e instanceof HttpError && e.status === 404) return getJson<RunRow[]>('/api/dev/runs')
        throw e
      }
    },
    refetchInterval: (q) => (q.state.data?.some((r) => r.state === 'running') ? 3000 : 15000),
  })
}

export function useReport(runId: number, live = false) {
  return useQuery({
    queryKey: ['pub-report', runId],
    queryFn: () => getJson<Report>(`/api/search/${runId}`),
    refetchInterval: live ? 5000 : false,
  })
}

export function useCorpus(runId: number) {
  return useQuery({
    queryKey: ['pub-corpus', runId],
    queryFn: () => getJson<EntrySeries[]>(`/api/search/${runId}/corpus`),
    retry: false,
    staleTime: 60 * 60 * 1000,
  })
}

export function useCorpusSeries(q: string | null) {
  return useQuery({
    queryKey: ['pub-corpus-q', q],
    queryFn: () => getJson<EntrySeries>(`/api/corpus/series?q=${encodeURIComponent(q ?? '')}&months=36`),
    enabled: !!q && q.trim().length > 1,
    retry: false,
    staleTime: 60 * 60 * 1000,
  })
}

export function useStopRun() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (runId: number) => {
      const r = await fetch(`/api/search/${runId}/stop`, { method: 'POST', headers: { accept: 'application/json' } })
      if (!r.ok) {
        let detail = (await r.text()).slice(0, 300)
        try { detail = JSON.parse(detail).detail ?? detail } catch { /* plain text */ }
        throw new HttpError(r.status, detail)
      }
      return (await r.json()) as { run_id: number; state: string | null }
    },
    onSuccess: () => { client.invalidateQueries({ queryKey: ['pub-runs'] }) },
  })
}

export function useResumeRun() {
  return useMutation({
    mutationFn: async ({ runId, max_cost_usd }: { runId: number; max_cost_usd?: number }) => {
      const r = await fetch(`/api/search/${runId}/resume`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify(max_cost_usd == null ? {} : { max_cost_usd }),
      })
      if (!r.ok) {
        let detail = (await r.text()).slice(0, 300)
        try { detail = JSON.parse(detail).detail ?? detail } catch { /* plain text */ }
        throw new HttpError(r.status, detail)
      }
      return (await r.json()) as { run_id: number }
    },
  })
}

export function useStartRun() {
  return useMutation({
    mutationFn: async (body: { query: string; max_research?: number; max_cost_usd?: number; top?: number }) => {
      const r = await fetch('/api/search', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify(body),
      })
      if (r.status === 409) throw new HttpError(409, 'Другое исследование уже выполняется. Дождитесь его окончания или откройте его из истории.')
      if (!r.ok) throw new HttpError(r.status, (await r.text()).slice(0, 300))
      return (await r.json()) as { run_id: number }
    },
  })
}

function parseBlock(block: string): RunEvent | null {
  let id = 0
  let name = ''
  const data: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('id:')) id = Number(line.slice(3).trim()) || 0
    else if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
  }
  if (!data.length && !name) return null
  let payload: Record<string, unknown> = {}
  try {
    payload = data.length ? JSON.parse(data.join('\n')) : {}
  } catch {
    payload = { raw: data.join('\n') }
  }
  return { ...payload, id, kind: String(payload.kind ?? name ?? 'message') }
}

export type StreamState = 'loading' | 'open' | 'reconnecting' | 'ended' | 'error'

export function useRunEvents(runId: number) {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [state, setState] = useState<StreamState>('loading')
  const [error, setError] = useState<string | null>(null)
  const buf = useRef<RunEvent[]>([])
  useEffect(() => {
    const ctl = new AbortController()
    buf.current = []
    let flushTimer = 0
    let retryTimer = 0
    let lastId = 0
    let failures = 0
    const flush = () => {
      flushTimer = 0
      setEvents([...buf.current])
    }
    const pause = (ms: number) => new Promise<void>((resolve) => { retryTimer = window.setTimeout(resolve, ms) })
    ;(async () => {
      for (;;) {
        let ended = false
        try {
          const r = await fetch(`/api/search/${runId}/events?after=${lastId}`, { signal: ctl.signal, headers: { accept: 'text/event-stream' } })
          if (!r.ok || !r.body) throw new HttpError(r.status, r.statusText)
          setState('open')
          setError(null)
          failures = 0
          const reader = r.body.getReader()
          const dec = new TextDecoder()
          let text = ''
          for (;;) {
            const { value, done } = await reader.read()
            if (done) break
            text += dec.decode(value, { stream: true }).replace(/\r\n/g, '\n')
            let cut = text.indexOf('\n\n')
            while (cut >= 0) {
              const ev = parseBlock(text.slice(0, cut))
              text = text.slice(cut + 2)
              if (ev) {
                if (ev.kind === 'end') ended = true
                if (ev.id > lastId) lastId = ev.id
                buf.current.push(ev)
              }
              cut = text.indexOf('\n\n')
            }
            if (!flushTimer) flushTimer = window.setTimeout(flush, 120)
          }
          flush()
          if (ended) {
            setState('ended')
            return
          }
          throw new Error('соединение закрылось до конца исследования')
        } catch (e) {
          if (ctl.signal.aborted) return
          flush()
          setError(e instanceof Error ? e.message : String(e))
          if (e instanceof HttpError && e.status === 404) {
            setState('error')
            return
          }
          failures += 1
          setState('reconnecting')
          await pause(Math.min(30000, 1000 * 2 ** Math.min(failures - 1, 5)))
          if (ctl.signal.aborted) return
        }
      }
    })()
    return () => {
      ctl.abort()
      if (flushTimer) window.clearTimeout(flushTimer)
      if (retryTimer) window.clearTimeout(retryTimer)
    }
  }, [runId])
  return { events, state, error }
}
