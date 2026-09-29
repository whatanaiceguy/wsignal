import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useAgentTurns, useRunAgents, useRunTurns } from '../api'
import type { TurnKind, TurnOut } from '../types'
import { TurnBlock } from './TurnBlock'
import { Empty, Failure, Loading } from './ui'

const KINDS: TurnKind[] = ['system', 'user', 'assistant', 'tool_call']

function pair(turns: TurnOut[]) {
  const results = new Map<string, TurnOut>()
  for (const turn of turns) {
    if (turn.kind === 'tool_result' && turn.content.id) results.set(turn.content.id, turn)
  }
  return turns
    .filter((turn) => turn.kind !== 'tool_result')
    .map((turn) => ({
      turn,
      result: turn.kind === 'tool_call' ? (results.get(turn.content.id ?? '') ?? null) : null,
    }))
}

export function Transcript({ agentId, runId, targetSeq }: { agentId: number | null; runId: number | null; targetSeq: number | null }) {
  const { t } = useTranslation()
  const [merged, setMerged] = useState(false)
  const [json, setJson] = useState(false)
  const [hidden, setHidden] = useState<Set<TurnKind>>(new Set())
  const [open, setOpen] = useState<Set<number>>(new Set())
  const [errorsOnly, setErrorsOnly] = useState(false)

  const agents = useRunAgents(runId, true)
  const single = useAgentTurns(merged ? null : agentId, true)
  const whole = useRunTurns(runId, true, merged)

  const roles = useMemo(
    () => new Map((agents.data ?? []).map((agent) => [agent.id, agent.role])),
    [agents.data],
  )

  const turns = useMemo(
    () => (merged ? (whole.data ?? []) : (single.data?.turns ?? [])),
    [merged, single.data?.turns, whole.data],
  )
  const blocks = useMemo(() => pair(turns), [turns])
  const maxMs = useMemo(
    () => Math.max(1, ...turns.map((turn) => turn.duration_ms ?? 0)),
    [turns],
  )

  useEffect(() => {
    if (targetSeq === null || merged) return
    document.getElementById(`transcript-seq-${targetSeq}`)?.scrollIntoView({ block: 'center' })
  }, [targetSeq, turns, merged])

  const visible = blocks.filter(({ turn, result }) => {
    if (hidden.has(turn.kind)) return false
    if (!errorsOnly) return true
    return Boolean(turn.content.error ?? result?.content.payload?.error)
  })

  if (!merged && agentId === null) return <Empty>{t('turn.empty')}</Empty>
  if (single.isLoading || whole.isLoading) return <Loading />
  if (single.error) return <Failure error={single.error} />
  if (whole.error) return <Failure error={whole.error} />

  const toggleKind = (kind: TurnKind) => {
    const next = new Set(hidden)
    if (next.has(kind)) next.delete(kind)
    else next.add(kind)
    setHidden(next)
  }

  return (
    <div>
      <div className="sticky top-0 z-10 flex flex-wrap items-center gap-1 border-b border-line bg-panel px-3 py-1.5">
        <button
          type="button"
          onClick={() => setMerged(!merged)}
          disabled={runId === null}
          className={`rounded border px-2 py-0.5 ${
            merged ? 'border-accent text-ink' : 'border-line text-ink-3'
          }`}
        >
          {merged ? t('turn.merged') : t('turn.single')}
        </button>
        <span className="mx-1 h-4 w-px bg-line" />
        {KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => toggleKind(kind)}
            className={`rounded border px-1.5 py-0.5 text-[11px] ${
              hidden.has(kind) ? 'border-line text-ink-3 line-through' : 'border-line text-ink-2'
            }`}
          >
            {t(`kind.${kind}`)}
          </button>
        ))}
        <button
          type="button"
          onClick={() => setErrorsOnly(!errorsOnly)}
          className={`rounded border px-1.5 py-0.5 text-[11px] ${
            errorsOnly ? 'border-live-failed text-live-failed' : 'border-line text-ink-3'
          }`}
        >
          {t('common.onlyErrors')}
        </button>
        <span className="mx-1 h-4 w-px bg-line" />
        <button
          type="button"
          onClick={() => setOpen(new Set(visible.map(({ turn }) => turn.id)))}
          className="rounded border border-line px-1.5 py-0.5 text-[11px] text-ink-3"
        >
          {t('turn.expandAll')}
        </button>
        <button
          type="button"
          onClick={() => setOpen(new Set())}
          className="rounded border border-line px-1.5 py-0.5 text-[11px] text-ink-3"
        >
          {t('turn.collapseAll')}
        </button>
        <button
          type="button"
          onClick={() => setJson(!json)}
          className={`ml-auto rounded border px-2 py-0.5 text-[11px] ${
            json ? 'border-accent text-ink' : 'border-line text-ink-3'
          }`}
        >
          {json ? t('turn.json') : t('turn.render')}
        </button>
      </div>

      {visible.length === 0 && <Empty>{t('common.none')}</Empty>}

      {visible.map(({ turn, result }) => (
        <div
          key={turn.id}
          id={!merged && (turn.seq === targetSeq || result?.seq === targetSeq) ? `transcript-seq-${targetSeq}` : undefined}
          className={!merged && (turn.seq === targetSeq || result?.seq === targetSeq) ? 'bg-raised' : undefined}
        >
          <TurnBlock
            turn={turn}
            result={result}
            role={roles.get(turn.agent_id)}
            showRole={merged}
            maxMs={maxMs}
            json={json}
            open={open.has(turn.id)}
            onToggle={() => {
              const next = new Set(open)
              if (next.has(turn.id)) next.delete(turn.id)
              else next.add(turn.id)
              setOpen(next)
            }}
          />
        </div>
      ))}
    </div>
  )
}
