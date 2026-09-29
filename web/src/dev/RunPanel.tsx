import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { post, useNotes, useRun, useRunAgents, useRunEntries, useRunErrors, useSourceEvents } from '../api'
import type { AgentOut, EntryOut, ErrorKind, HarnessNote, Role, RunError } from '../types'
import { Dot, Empty, Failure, Loading, RoleTag, entryText, fmt } from './ui'

type Tab = 'agents' | 'errors' | 'notes' | 'entries' | 'sources' | 'meta'
const TABS: Tab[] = ['agents', 'errors', 'notes', 'entries', 'sources', 'meta']
const ERROR_KINDS: ErrorKind[] = ['source', 'agent', 'model', 'tool', 'refutation', 'run', 'fetch', 'citation']
const ROLES: Role[] = ['orchestrator', 'assistant', 'researcher', 'refuter']

const PROSE: (keyof EntryOut)[] = [
  'problem_ru',
  'advantage_ru',
  'case_example_ru',
  'current_state_ru',
  'dynamics_ru',
  'what_would_refute_ru',
]

function AgentRow({
  agent,
  depth,
  selected,
  onSelect,
}: {
  agent: AgentOut
  depth: number
  selected: number | null
  onSelect: (id: number) => void
}) {
  const { t } = useTranslation()
  return (
    <button
      type="button"
      onClick={() => onSelect(agent.id)}
      style={{ paddingLeft: `${8 + depth * 12}px` }}
      className={`block w-full border-b border-line-soft py-1.5 pr-2 text-left hover:bg-raised ${
        selected === agent.id ? 'bg-raised' : ''
      }`}
    >
      <div className="flex items-center gap-2">
        <Dot state={agent.state} live={agent.is_live} />
        {agent.state === 'running' && !agent.is_live && (
          <span className="text-[10px] text-live-failed">{t('state.stale')}</span>
        )}
        <RoleTag role={agent.role} id={agent.id} />
        <span className="truncate text-[11px] text-ink-3">{fmt.model(agent.model)}</span>
        <span className="num ml-auto shrink-0 text-[11px] text-ink-3">
          {agent.metrics.model_call_budget === null
            ? t('count.modelCall', { count: agent.metrics.model_calls })
            : t('count.modelCallBudget', {
                count: agent.metrics.model_calls,
                budget: agent.metrics.model_call_budget,
              })}
        </span>
      </div>
      <div className="num mt-0.5 flex gap-2 text-[11px] text-ink-3">
        <span>{fmt.usd(agent.metrics.cost_usd)}</span>
        <span>{t('count.turn', { count: agent.metrics.turns })}</span>
        <span>{fmt.secs(agent.metrics.wall_time_s)}</span>
        {agent.metrics.error_turns > 0 && (
          <span className="text-live-failed">{agent.metrics.error_turns}</span>
        )}
      </div>
      {agent.field && <div className="mt-0.5 truncate text-ink-2">{agent.field}</div>}
    </button>
  )
}

function AgentTree({
  agents,
  selected,
  onSelect,
}: {
  agents: AgentOut[]
  selected: number | null
  onSelect: (id: number) => void
}) {
  const children = new Map<number | null, AgentOut[]>()
  for (const agent of agents) {
    const key = agents.some((other) => other.id === agent.parent_agent_id)
      ? agent.parent_agent_id
      : null
    const bucket = children.get(key) ?? []
    bucket.push(agent)
    children.set(key, bucket)
  }
  const render = (parent: number | null, depth: number): React.ReactNode[] =>
    (children.get(parent) ?? []).flatMap((agent) => [
      <AgentRow
        key={agent.id}
        agent={agent}
        depth={depth}
        selected={selected}
        onSelect={onSelect}
      />,
      ...render(agent.id, depth + 1),
    ])
  return <div>{render(null, 0)}</div>
}

function Entries({ runId, onSelect }: { runId: number; onSelect: (id: number) => void }) {
  const { t } = useTranslation()
  const { data, isLoading, error } = useRunEntries(runId)
  if (isLoading) return <Loading />
  if (error) return <Failure error={error} />
  if (!data?.entries.length) return <Empty>{t('common.none')}</Empty>

  return (
    <div>
      {[...data.entries]
        .sort((a, b) => {
          if (a.weak_score === null && b.weak_score !== null) return 1
          if (a.weak_score !== null && b.weak_score === null) return -1
          return (b.weak_score ?? b.score) - (a.weak_score ?? a.score) || b.score - a.score
        })
        .map((entry) => (
          <div key={entry.id} className="border-b border-line-soft px-2 py-2">
            <div className="flex items-center gap-2">
              <span className="num text-ink">
                {entry.weak_score === null ? '—' : entry.weak_score.toFixed(3)}
              </span>
              <span className="num text-[10px] text-ink-3">Score {entry.score.toFixed(2)}</span>
              <span className="text-[11px] text-ink-3">{entry.signal_class ?? '—'}</span>
              <span className="num text-[10px] text-ink-3">
                S {entry.substance ?? '—'} · M {entry.momentum ?? '—'} · F {entry.faintness ?? '—'}
              </span>
              <span className={`text-[11px] ${entryText[entry.state]}`}>
                {t(`entryState.${entry.state}`)}
              </span>
              <span className="num ml-auto text-[11px] text-ink-3">
                {t('count.citation', { count: entry.citations.length })}
              </span>
            </div>
            <div className="mt-1 text-ink">{entry.name_ru}</div>
            {entry.entry_patterns.length > 0 && (
              <div className="mt-1 text-[10px] text-ink-3">
                {entry.entry_patterns.map((pattern) =>
                  `${pattern.kind}:${pattern.pattern} ${pattern.strength ?? '—'}`,
                ).join(' · ')}
              </div>
            )}
            <div className="mt-1 flex flex-wrap gap-1">
              {PROSE.map((field) => (
                <span
                  key={field}
                  title={field}
                  className={`h-1.5 w-6 rounded-full ${
                    entry[field] ? 'bg-entry-banked' : 'bg-live-failed/50'
                  }`}
                />
              ))}
            </div>
            <div className="num mt-1 flex gap-2 text-[11px] text-ink-3">
              {entry.researched_by_agent_id && (
                <button
                  type="button"
                  className="text-accent"
                  onClick={() => onSelect(entry.researched_by_agent_id!)}
                >
                  #{entry.researched_by_agent_id}
                </button>
              )}
              {entry.refuter_agent_id && (
                <button
                  type="button"
                  className="text-accent"
                  onClick={() => onSelect(entry.refuter_agent_id!)}
                >
                  #{entry.refuter_agent_id}
                </button>
              )}
              <span className="ml-auto">
                {entry.searches_run} / {entry.sources_checked}
              </span>
            </div>
          </div>
        ))}
    </div>
  )
}

function Sources({ runId, onSelect }: { runId: number; onSelect: (id: number) => void }) {
  const { t } = useTranslation()
  const { data, isLoading, error } = useSourceEvents(runId)
  if (isLoading) return <Loading />
  if (error) return <Failure error={error} />
  if (!data?.length) return <Empty>{t('common.none')}</Empty>

  return (
    <div className="num text-[11px]">
      {data.map((event) => (
        <div key={event.id} className="border-b border-line-soft px-2 py-1">
          <div className="flex items-center gap-2">
            <span className={event.ok ? 'text-ink-2' : 'text-live-failed'}>{event.adapter}</span>
            {event.via && <span className="text-ink-3">via {event.via}</span>}
            <span className="ml-auto text-ink-3">{event.items ?? '—'}</span>
            <span className="w-12 text-right text-ink-3">{fmt.ms(event.duration_ms)}</span>
            <span className="w-14 text-right text-ink-3">{fmt.bytes(event.bytes)}</span>
          </div>
          {event.query && <div className="truncate text-ink-3">{event.query}</div>}
          {event.error && <div className="text-live-failed">{event.error}</div>}
          {event.agent_id && (
            <button type="button" className="text-accent" onClick={() => onSelect(event.agent_id!)}>
              #{event.agent_id}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

function Errors({ runId, onSelect }: { runId: number; onSelect: (id: number, turnSeq?: number) => void }) {
  const { t } = useTranslation()
  const [kinds, setKinds] = useState<ErrorKind[]>([])
  const [role, setRole] = useState<Role | ''>('')
  const [query, setQuery] = useState('')
  const { data, isLoading, error } = useRunErrors(runId, {
    kind: kinds.length ? kinds : undefined,
    role: role || undefined,
    q: query || undefined,
  })
  if (isLoading) return <Loading />
  if (error) return <Failure error={error} />
  if (!data) return null

  const toggleKind = (kind: ErrorKind) => {
    setKinds((current) => current.includes(kind)
      ? current.filter((item) => item !== kind)
      : [...current, kind])
  }

  return (
    <div>
      <div className="sticky top-8 z-10 border-b border-line bg-panel p-2">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('common.filter')}
          className="w-full rounded border border-line bg-sunken px-2 py-1 text-ink outline-none focus:border-accent"
        />
        <div className="mt-2 flex flex-wrap gap-1">
          {ERROR_KINDS.map((kind) => (
            <button
              key={kind}
              type="button"
              onClick={() => toggleKind(kind)}
              className={`num rounded border px-1.5 py-0.5 text-[11px] ${
                kinds.includes(kind) ? 'border-live-failed text-live-failed' : 'border-line text-ink-3'
              }`}
            >
              {t(`errors.kind.${kind}`)} {data.counts[kind] ?? 0}
            </button>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap gap-1">
          <button
            type="button"
            onClick={() => setRole('')}
            className={`rounded border px-1.5 py-0.5 text-[11px] ${
              role === '' ? 'border-accent text-ink' : 'border-line text-ink-3'
            }`}
          >
            {t('errors.allRoles')}
          </button>
          {ROLES.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setRole(role === item ? '' : item)}
              className={`rounded border px-1.5 py-0.5 text-[11px] ${
                role === item ? 'border-accent text-ink' : 'border-line text-ink-3'
              }`}
            >
              {t(`roleShort.${item}`)}
            </button>
          ))}
        </div>
      </div>
      {data.errors.length === 0 && <Empty>{t('common.none')}</Empty>}
      {data.errors.map((item) => (
        <ErrorRow key={item.id} item={item} onSelect={onSelect} />
      ))}
    </div>
  )
}

function Notes({ runId, onSelect }: { runId: number; onSelect: (id: number) => void }) {
  const { t } = useTranslation()
  const [scope, setScope] = useState<'run' | 'all'>('run')
  const { data, isLoading, error } = useNotes(scope === 'run' ? runId : null)
  if (isLoading) return <Loading />
  if (error) return <Failure error={error} />
  const notes = data ?? []
  return (
    <div>
      <div className="sticky top-8 z-10 flex gap-1 border-b border-line bg-panel p-2 text-[11px]">
        {(['run', 'all'] as const).map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setScope(item)}
            className={`rounded border px-1.5 py-0.5 ${scope === item ? 'border-accent text-ink' : 'border-line text-ink-3'}`}
          >
            {item === 'run' ? `#${runId}` : 'all runs'}
          </button>
        ))}
        <span className="num ml-auto text-ink-3">{notes.length}</span>
      </div>
      {notes.length === 0 && <Empty>{t('common.none')}</Empty>}
      {notes.map((note) => <NoteRow key={note.id} note={note} onSelect={onSelect} />)}
    </div>
  )
}

function NoteRow({ note, onSelect }: { note: HarnessNote; onSelect: (id: number) => void }) {
  const last = note.meta?.last_call
  return (
    <div className="border-b border-line-soft px-2 py-2 text-[11px]">
      <div className="flex items-center gap-2">
        <time className="num shrink-0 text-[10px] text-ink-3" dateTime={note.created_at}>{fmt.utcClock(note.created_at)}</time>
        <span className={`rounded px-1 ${note.kind === 'bug' ? 'bg-live-failed/10 text-live-failed' : 'bg-raised text-ink-2'}`}>{note.kind}</span>
        {note.run_id !== null && <span className="num text-ink-3">run {note.run_id}</span>}
        {note.agent_id !== null && (
          <button type="button" className="num text-accent" onClick={() => onSelect(note.agent_id!)}>#{note.agent_id}</button>
        )}
        {note.role && <span className="text-ink-3">{note.role}</span>}
        {note.tool && <span className="text-ink-2">{note.tool}</span>}
        <span className="truncate text-ink-3">{fmt.model(note.model ?? '')}</span>
      </div>
      <div className="mt-1 whitespace-pre-wrap break-words text-ink">{note.text}</div>
      {last && (
        <details className="mt-1 text-ink-3">
          <summary>last call: {last.name}</summary>
          <div className="whitespace-pre-wrap break-words">args {last.arguments}</div>
          <div className="whitespace-pre-wrap break-words">result {last.result}</div>
        </details>
      )}
    </div>
  )
}

function ErrorRow({ item, onSelect }: { item: RunError; onSelect: (id: number, turnSeq?: number) => void }) {
  const { t } = useTranslation()
  const content = (
    <>
      <div className="flex items-center gap-2">
        <time className="num shrink-0 text-[10px] text-ink-3" dateTime={item.created_at}>
          {fmt.utcClock(item.created_at)}
        </time>
        <span className="rounded bg-live-failed/10 px-1 text-live-failed">{t(`errors.kind.${item.kind}`)}</span>
        {item.agent_id !== null && <span className="num text-ink-3">#{item.agent_id}</span>}
        {item.role && <span className="text-ink-3">{t(`roleShort.${item.role}`)}</span>}
        <span className="truncate text-ink">{item.title}</span>
      </div>
      <div className="mt-1 whitespace-pre-wrap break-words text-live-failed">{item.message}</div>
    </>
  )
  return item.agent_id !== null ? (
    <button
      type="button"
      onClick={() => onSelect(item.agent_id!, item.turn_seq ?? undefined)}
      className="block w-full border-b border-line-soft px-2 py-2 text-left text-[11px] hover:bg-raised"
    >
      {content}
    </button>
  ) : (
    <div className="border-b border-line-soft px-2 py-2 text-[11px]">{content}</div>
  )
}

function OperatorMessage({ runId, live }: { runId: number; live: boolean }) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const client = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => post(
      `/runs/${runId}/${live ? 'message' : 'resume'}`,
      live ? { text: text.trim() } : { message: text.trim() },
    ),
    onSuccess: () => {
      setText('')
      void client.invalidateQueries({ queryKey: ['run', runId] })
      void client.invalidateQueries({ queryKey: ['runs'] })
      void client.invalidateQueries({ queryKey: ['run-agents', runId] })
    },
  })
  return (
    <form className="space-y-2 border-b border-line p-2" onSubmit={(event) => {
      event.preventDefault()
      if (text.trim() && !mutation.isPending) mutation.mutate()
    }}>
      <label className="block text-[11px] text-ink-2" htmlFor="operator-message">
        {t('operator.message')}
      </label>
      <textarea
        id="operator-message"
        value={text}
        maxLength={20000}
        disabled={mutation.isPending}
        onChange={(event) => { setText(event.target.value); mutation.reset() }}
        className="w-full rounded border border-line bg-panel p-2 text-[12px] text-ink"
        rows={3}
      />
      <button
        type="submit"
        disabled={!text.trim() || mutation.isPending}
        className="rounded border border-line px-2 py-1 text-[11px] text-ink disabled:opacity-40"
      >
        {t(live ? 'operator.send' : 'operator.resume')}
      </button>
      {mutation.isSuccess && <p role="status" className="text-[11px] text-ink-2">{t('operator.queued')}</p>}
      {mutation.error && <Failure error={mutation.error} />}
    </form>
  )
}

function Meta({ runId }: { runId: number }) {
  const { data, isLoading, error } = useRun(runId)
  if (isLoading) return <Loading />
  if (error) return <Failure error={error} />
  if (!data) return null
  return (
    <>
      <OperatorMessage key={runId} runId={runId} live={data.run.is_live} />
      <pre className="overflow-auto p-2 font-mono text-[11px] text-ink-2">
        {JSON.stringify(data.meta, null, 2)}
      </pre>
    </>
  )
}

export function RunPanel({
  runId,
  selectedAgent,
  onSelectAgent,
}: {
  runId: number | null
  selectedAgent: number | null
  onSelectAgent: (id: number, runId?: number | null, turnSeq?: number) => void
}) {
  const { t } = useTranslation()
  const [tab, setTab] = useState<Tab>('agents')
  const agents = useRunAgents(runId, true)

  if (runId === null) return <Empty>{t('nav.runs')}</Empty>

  const select = (id: number, turnSeq?: number) => onSelectAgent(id, runId, turnSeq)

  return (
    <div>
      <div className="sticky top-0 z-10 flex gap-1 border-b border-line bg-panel px-2 py-1.5">
        {TABS.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setTab(item)}
            className={`rounded px-2 py-0.5 text-[11px] ${
              tab === item ? 'bg-raised text-ink' : 'text-ink-3'
            }`}
          >
            {t(`tabs.${item}`)}
          </button>
        ))}
      </div>

      {tab === 'agents' &&
        (agents.isLoading ? (
          <Loading />
        ) : agents.error ? (
          <Failure error={agents.error} />
        ) : (
          <AgentTree agents={agents.data ?? []} selected={selectedAgent} onSelect={select} />
        ))}
      {tab === 'errors' && <Errors runId={runId} onSelect={select} />}
      {tab === 'notes' && <Notes runId={runId} onSelect={(id) => select(id)} />}
      {tab === 'entries' && <Entries runId={runId} onSelect={select} />}
      {tab === 'sources' && <Sources runId={runId} onSelect={select} />}
      {tab === 'meta' && <Meta runId={runId} />}
    </div>
  )
}
