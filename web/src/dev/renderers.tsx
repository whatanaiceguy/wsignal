import type { JSX, ReactNode } from 'react'
import { Link } from 'react-router-dom'

const AGENT_KEYS = [
  'agent_id',
  'assistant_agent_id',
  'refuter_agent_id',
  'attacked_agent_id',
  'researched_by_agent_id',
  'written_by_agent_id',
  'fetched_by_agent_id',
]

type Data = Record<string, unknown>

const scalar = (value: unknown): string => {
  if (value == null) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value) ?? String(value)
}

const firstClause = (value: unknown): string => scalar(value).split(/[.!?\n]/, 1)[0] || scalar(value)

const number = (value: unknown): ReactNode =>
  typeof value === 'number' ? <span className="num">{value}</span> : scalar(value)

const agent = (id: unknown): ReactNode => {
  if (typeof id !== 'number' && typeof id !== 'string') return scalar(id)
  return <Link className="text-accent hover:underline" to={`/dev/agent/${id}`}>{id}</Link>
}

const title = (value: unknown): string => scalar(value)

function Cell({ value, children, className = '' }: { value?: ReactNode; children?: ReactNode; className?: string }) {
  return <div className={`min-w-0 break-words ${className}`}>{value ?? children}</div>
}

function Value({ value, keyName }: { value: unknown; keyName?: string }): ReactNode {
  if (keyName && AGENT_KEYS.includes(keyName)) {
    return agent(value)
  }
  if (typeof value === 'number') return number(value)
  if (Array.isArray(value)) return value.map((item, index) => <div key={index}>{item && typeof item === 'object' ? <Table data={item as Data} /> : scalar(item)}</div>)
  if (value && typeof value === 'object') return <Table data={value as Data} />
  return scalar(value)
}

function Table({ data, omit = [] }: { data: Data; omit?: string[] }) {
  return (
    <div className="divide-y divide-line-soft border border-line-soft">
      {Object.entries(data).filter(([key]) => !omit.includes(key)).map(([key, value]) => (
        <div className="grid grid-cols-[minmax(9rem,0.7fr)_minmax(0,1.5fr)] gap-3 px-3 py-1.5" key={key}>
          <Cell className="text-ink-3" value={key} />
          <Cell className={key === 'sources_failed' ? 'text-live-failed' : ''} value={<Value keyName={key} value={value} />} />
        </div>
      ))}
    </div>
  )
}

function StatusNote({ payload }: { payload: Data }) {
  const status = payload._status
  const note = payload.note
  if (status == null && note == null) return null
  return (
    <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 border-l-2 border-accent bg-sunken px-3 py-2">
      {status != null && <span><span className="text-ink-3">status </span><span className="text-accent">{scalar(status)}</span></span>}
      {note != null && <span className="whitespace-pre-wrap text-ink-2">{scalar(note)}</span>}
    </div>
  )
}

function SearchPayload({ payload }: { payload: Data }) {
  const records = Array.isArray(payload.records) ? payload.records : []
  return (
    <div className="space-y-3">
      <Table data={payload} omit={['records', 'note']} />
      <div className="overflow-x-auto border border-line-soft">
        <div className="grid min-w-[42rem] grid-cols-[minmax(14rem,2fr)_minmax(8rem,1fr)_7rem_8rem] gap-3 border-b border-line-soft bg-sunken px-3 py-1.5 text-ink-3">
          <span>title</span><span>host / url</span><span>date</span><span>adapter</span>
        </div>
        {records.map((item, index) => {
          const record = item && typeof item === 'object' ? item as Data : {}
          const url = typeof record.url === 'string' ? record.url : ''
          const host = record.host ?? (url ? (() => { try { return new URL(url).host } catch { return url } })() : '—')
          return (
            <div className="grid min-w-[42rem] grid-cols-[minmax(14rem,2fr)_minmax(8rem,1fr)_7rem_8rem] gap-3 border-b border-line-soft px-3 py-2 last:border-b-0" key={index}>
              <Cell value={url ? <a className="text-accent hover:underline" href={url} target="_blank" rel="noreferrer">{title(record.title)}</a> : title(record.title)} />
              <Cell value={url ? <a className="text-accent hover:underline" href={url} target="_blank" rel="noreferrer">{scalar(host)}</a> : scalar(host)} />
              <Cell value={scalar(record.date ?? record.published_at)} />
              <Cell value={scalar(record.adapter ?? payload.adapter)} />
            </div>
          )
        })}
      </div>
    </div>
  )
}

function FetchPayload({ payload }: { payload: Data }) {
  const metadata = { url: payload.url, title: payload.title, source_name: payload.source_name, source_tier: payload.source_tier, published_at: payload.published_at, chars_from: payload.chars_from, chars_to: payload.chars_to, chars_total: payload.chars_total, links_to: payload.links_to, from_store: payload.from_store, note: payload.note }
  return (
    <div className="space-y-3">
      <Table data={metadata} />
      <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap border border-line-soft bg-sunken p-3 font-mono text-ink-2">{scalar(payload.content)}</pre>
    </div>
  )
}

function ErrorPayload({ payload }: { payload: Data }) {
  return (
    <div className="space-y-3">
      <div className="border border-live-failed/40 bg-live-failed/10 p-3 text-live-failed">{scalar(payload.error)}</div>
      {payload.arguments != null && <Table data={payload.arguments && typeof payload.arguments === 'object' ? payload.arguments as Data : { arguments: payload.arguments }} />}
    </div>
  )
}

function KnownTechnologies({ payload }: { payload: Data }) {
  return <div className="space-y-3"><Table data={payload} omit={['matches', 'note']} />{payload.matches != null && <div className="border border-line-soft p-3"><Value value={payload.matches} /></div>}</div>
}

function ListSources({ payload }: { payload: Data }) {
  return <div className="space-y-2">{Array.isArray(payload.sources) ? payload.sources.map((source, index) => <div className="border border-line-soft p-2" key={index}><Value value={source} /></div>) : <Table data={payload} />}</div>
}

function ShapedPayload({ name, payload }: { name: string; payload: Data }) {
  if (name === 'search') return <SearchPayload payload={payload} />
  if (name === 'fetch') return <FetchPayload payload={payload} />
  if (name === 'known_technologies') return <KnownTechnologies payload={payload} />
  if (name === 'list_sources') return <ListSources payload={payload} />
  if (name === 'error') return <ErrorPayload payload={payload} />
  return <Table data={payload} omit={['_status', 'note']} />
}

export function summarise(name: string, args: Data | undefined, payload: Data | undefined): string {
  if (payload?.error != null) return firstClause(payload.error)
  if (name === 'search') return `${scalar(args?.query ?? payload?.query)} · ${scalar(payload?.adapter ?? args?.adapter)} · ${scalar(payload?.count)} / ${scalar(payload?.matched_total)}`
  if (name === 'fetch') {
    let host = scalar(payload?.url)
    try { host = new URL(String(payload?.url)).host } catch { }
    return `${host} · ${scalar(payload?.title)} · ${scalar(payload?.chars_total)} chars`
  }
  const source = args ?? payload ?? {}
  const entry = Object.entries(source).find(([, value]) => value !== null && ['string', 'number', 'boolean'].includes(typeof value))
  return entry ? `${name} · ${entry[1]}` : name || 'tool'
}

export function ToolArguments({ name, args }: { name: string; args?: Data }): JSX.Element {
  return <div className="space-y-2"><div className="text-[11px] uppercase tracking-wide text-ink-3">{name} arguments</div>{args && Object.keys(args).length > 0 ? <Table data={args} /> : <div className="text-ink-3">—</div>}</div>
}

export function ToolPayload({ name, payload }: { name: string; payload?: Data }): JSX.Element {
  if (!payload) return <div className="text-ink-3">—</div>
  const isError = payload.error != null
  return <div className="space-y-3"><StatusNote payload={payload} />{isError ? <ErrorPayload payload={payload} /> : <ShapedPayload name={name} payload={payload} />}</div>
}

