import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useTranscriptSearch } from '../api'
import { Failure, Loading, RoleTag, fmt } from './ui'

export function TranscriptSearch({
  runId,
  onSelect,
}: {
  runId: number | null
  onSelect: (agentId: number, runId?: number | null) => void
}) {
  const { t } = useTranslation()
  const [term, setTerm] = useState('')
  const [scoped, setScoped] = useState(true)
  const { data, isLoading, error } = useTranscriptSearch(term, scoped ? runId : null)

  return (
    <div>
      <div className="sticky top-0 z-10 border-b border-line bg-panel p-2">
        <input
          value={term}
          onChange={(event) => setTerm(event.target.value)}
          placeholder={t('nav.search')}
          className="w-full rounded border border-line bg-sunken px-2 py-1 text-ink outline-none focus:border-accent"
        />
        {runId !== null && (
          <label className="mt-2 flex items-center gap-2 text-[11px] text-ink-3">
            <input type="checkbox" checked={scoped} onChange={() => setScoped(!scoped)} />
            <span className="num">#{runId}</span>
          </label>
        )}
        {data && (
          <div className="num mt-2 text-[11px] text-ink-3">
            {t('count.hit', { count: data.length })}
          </div>
        )}
      </div>

      {isLoading && <Loading />}
      {error && <Failure error={error} />}

      {data?.map((hit) => (
        <button
          key={hit.id}
          type="button"
          onClick={() => onSelect(hit.agent_id, hit.run_id)}
          className="block w-full border-b border-line-soft px-2 py-1.5 text-left hover:bg-raised"
        >
          <div className="flex items-center gap-2">
            <RoleTag role={hit.role} id={hit.agent_id} />
            <span className="num text-[11px] text-ink-3">
              #{hit.run_id} / {hit.seq} / {t(`kind.${hit.kind}`)}
            </span>
            <span className="num ml-auto text-[11px] text-ink-3">{fmt.clock(hit.created_at)}</span>
          </div>
          <div className="mt-1 line-clamp-3 font-mono text-[11px] break-all text-ink-2">
            {hit.snippet}
          </div>
        </button>
      ))}
    </div>
  )
}
