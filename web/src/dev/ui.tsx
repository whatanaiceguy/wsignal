import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import type { AgentState, EntryState, Role, RunState, TurnKind } from '../types'

export const roleText: Record<Role, string> = {
  orchestrator: 'text-role-orchestrator',
  assistant: 'text-role-assistant',
  researcher: 'text-role-researcher',
  refuter: 'text-role-refuter',
}

export const roleBg: Record<Role, string> = {
  orchestrator: 'bg-role-orchestrator',
  assistant: 'bg-role-assistant',
  researcher: 'bg-role-researcher',
  refuter: 'bg-role-refuter',
}

export const liveBg: Record<RunState | AgentState, string> = {
  running: 'bg-live-running',
  finished: 'bg-live-done',
  done: 'bg-live-done',
  idle: 'bg-live-idle',
  failed: 'bg-live-failed',
  exhausted: 'bg-live-failed',
}

export const entryText: Record<EntryState, string> = {
  banked: 'text-entry-banked',
  parked: 'text-entry-parked',
  noise: 'text-entry-noise',
  insufficient: 'text-entry-insufficient',
}

export const kindBorder: Record<TurnKind, string> = {
  system: 'border-l-kind-system',
  user: 'border-l-kind-user',
  assistant: 'border-l-kind-assistant',
  tool_call: 'border-l-kind-tool',
  tool_result: 'border-l-kind-tool',
}

export const fmt = {
  usd: (value: number | null | undefined) => (value ? `$${value.toFixed(value < 1 ? 4 : 2)}` : '$0'),
  secs: (value: number | null | undefined) => {
    if (!value) return '0s'
    if (value < 60) return `${value.toFixed(1)}s`
    const minutes = Math.floor(value / 60)
    return `${minutes}m ${Math.round(value % 60)}s`
  },
  ms: (value: number | null | undefined) => (value == null ? '' : fmt.secs(value / 1000)),
  bytes: (value: number | null | undefined) => {
    if (!value) return '0'
    const units = ['B', 'KB', 'MB', 'GB']
    let size = value
    let unit = 0
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024
      unit += 1
    }
    return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`
  },
  clock: (iso: string) => new Date(iso).toLocaleTimeString(),
  utcClock: (iso: string) =>
    new Date(iso).toLocaleTimeString('en-GB', { timeZone: 'UTC', hour12: false }),
  stamp: (iso: string) => new Date(iso).toLocaleString(),
  model: (name: string) => name.split('/').pop() ?? name,
}

export function Dot({ state, live = state === 'running' }: { state: RunState | AgentState; live?: boolean }) {
  const shownState = state === 'running' && !live ? 'failed' : state
  const pulse = live ? 'animate-pulse' : ''
  return <span className={`inline-block size-2 shrink-0 rounded-full ${liveBg[shownState]} ${pulse}`} />
}

export function RoleTag({ role, id }: { role: Role; id?: number }) {
  const { t } = useTranslation()
  return (
    <span className={`num text-[11px] ${roleText[role]}`}>
      {t(`roleShort.${role}`)}
      {id != null && <span className="text-ink-3">#{id}</span>}
    </span>
  )
}

export function Bar({ value, max }: { value: number; max: number }) {
  const ratio = max > 0 ? Math.min(1, value / max) : 0
  const heavy = ratio > 0.6
  return (
    <span className="inline-block h-1 w-16 shrink-0 rounded-full bg-line-soft align-middle">
      <span
        className={`block h-1 rounded-full ${heavy ? 'bg-live-failed' : 'bg-line'}`}
        style={{ width: `${Math.max(2, ratio * 100)}%` }}
      />
    </span>
  )
}

export function Section({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div className="border-b border-line-soft">
      <div className="px-3 py-1.5 text-[11px] tracking-wide text-ink-3 uppercase">{title}</div>
      <div className="px-3 pb-3">{children}</div>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-6 text-center text-ink-3">{children}</div>
}

export function Loading() {
  const { t } = useTranslation()
  return <Empty>{t('common.loading')}</Empty>
}

export function Failure({ error }: { error: unknown }) {
  const { t } = useTranslation()
  return (
    <div className="m-3 rounded border border-live-failed/40 bg-live-failed/10 p-3 text-live-failed">
      {t('common.error')}: {error instanceof Error ? error.message : String(error)}
    </div>
  )
}
