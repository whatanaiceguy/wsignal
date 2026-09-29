import { useTranslation } from 'react-i18next'
import { useRuns } from '../api'
import { Dot, Failure, Loading, fmt } from './ui'

export function RunList({
  selected,
  onSelect,
}: {
  selected: number | null
  onSelect: (id: number) => void
}) {
  const { t } = useTranslation()
  const { data, isLoading, error } = useRuns()

  if (isLoading) return <Loading />
  if (error) return <Failure error={error} />

  return (
    <div>
      {data?.map((run) => (
        <button
          key={run.id}
          type="button"
          onClick={() => onSelect(run.id)}
          className={`block w-full border-b border-line-soft px-2 py-2 text-left hover:bg-raised ${
            selected === run.id ? 'bg-raised' : ''
          }`}
        >
          <div className="flex items-center gap-2">
            <Dot state={run.state} live={run.is_live} />
            {run.state === 'running' && !run.is_live && (
              <span className="text-[10px] text-live-failed">{t('state.stale')}</span>
            )}
            <span className="num text-ink-2">#{run.id}</span>
            <span className="num ml-auto text-ink-3">{fmt.usd(run.counts.cost_usd)}</span>
          </div>
          <div className="mt-1 truncate text-ink" title={run.query}>
            {run.query}
          </div>
          <div className="num mt-1 flex gap-2 text-[11px] text-ink-3">
            <span>{t('count.agent', { count: run.counts.agents })}</span>
            <span>{t('count.entry', { count: run.counts.entries })}</span>
            <span className="ml-auto">{fmt.secs(run.elapsed_s)}</span>
          </div>
        </button>
      ))}
    </div>
  )
}
