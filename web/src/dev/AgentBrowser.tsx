import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useAgents } from '../api'
import type { Role } from '../types'
import { Dot, Failure, Loading, RoleTag, fmt } from './ui'

const ROLES: Role[] = ['orchestrator', 'assistant', 'researcher', 'refuter']

export function AgentBrowser({
  selected,
  onSelect,
}: {
  selected: number | null
  onSelect: (id: number, runId?: number | null) => void
}) {
  const { t } = useTranslation()
  const [role, setRole] = useState<Role | ''>('')
  const [query, setQuery] = useState('')
  const [errorsOnly, setErrorsOnly] = useState(false)

  const { data, isLoading, error } = useAgents(
    { role: role || undefined, q: query || undefined, has_error: errorsOnly || undefined },
    true,
  )

  return (
    <div>
      <div className="sticky top-0 z-10 border-b border-line bg-panel p-2">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('common.filter')}
          className="w-full rounded border border-line bg-sunken px-2 py-1 text-ink outline-none focus:border-accent"
        />
        <div className="mt-2 flex flex-wrap gap-1">
          {ROLES.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setRole(role === item ? '' : item)}
              className={`num rounded border px-1.5 py-0.5 text-[11px] ${
                role === item ? 'border-accent text-ink' : 'border-line text-ink-3'
              }`}
            >
              {t(`roleShort.${item}`)}
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
        </div>
      </div>

      {isLoading && <Loading />}
      {error && <Failure error={error} />}

      {data?.map((agent) => (
        <button
          key={agent.id}
          type="button"
          onClick={() => onSelect(agent.id, agent.run_id)}
          className={`block w-full border-b border-line-soft px-2 py-1.5 text-left hover:bg-raised ${
            selected === agent.id ? 'bg-raised' : ''
          }`}
        >
          <div className="flex items-center gap-2">
            <Dot state={agent.state} live={agent.is_live} />
            {agent.state === 'running' && !agent.is_live && (
              <span className="text-[10px] text-live-failed">{t('state.stale')}</span>
            )}
            <RoleTag role={agent.role} id={agent.id} />
            <span className="num ml-auto text-[11px] text-ink-3">
              {agent.metrics.model_call_budget === null
                ? t('count.modelCall', { count: agent.metrics.model_calls })
                : t('count.modelCallBudget', {
                    count: agent.metrics.model_calls,
                    budget: agent.metrics.model_call_budget,
                  })}
            </span>
          </div>
          <div className="mt-0.5 flex gap-2 text-[11px] text-ink-3">
            <span className="num">#{agent.run_id}</span>
            <span className="truncate">{fmt.model(agent.model)}</span>
            <span>{t('count.turn', { count: agent.metrics.turns })}</span>
            {agent.metrics.error_turns > 0 && (
              <span className="num ml-auto text-live-failed">{agent.metrics.error_turns}</span>
            )}
          </div>
          {agent.field && <div className="mt-0.5 truncate text-ink-2">{agent.field}</div>}
        </button>
      ))}
    </div>
  )
}
