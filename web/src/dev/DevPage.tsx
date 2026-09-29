import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { setLanguage } from '../i18n'
import { AgentBrowser } from './AgentBrowser'
import { RunList } from './RunList'
import { RunPanel } from './RunPanel'
import { TranscriptSearch } from './TranscriptSearch'
import { Transcript } from './Transcript'

type Rail = 'runs' | 'agents' | 'search'

export function DevPage() {
  const { t, i18n } = useTranslation()
  const params = useParams()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [rail, setRail] = useState<Rail>('runs')

  const runId = params.runId ? Number(params.runId) : null
  const agentId = params.agentId ? Number(params.agentId) : null

  const openRun = (id: number) => navigate(`/dev/run/${id}`)
  const openAgent = (id: number, run?: number | null, turnSeq?: number) =>
    navigate(`${run ? `/dev/run/${run}/agent/${id}` : `/dev/agent/${id}`}${turnSeq ? `?seq=${turnSeq}` : ''}`)

  return (
    <div className="flex h-full flex-col bg-base">
      <header className="flex h-9 shrink-0 items-center gap-1 border-b border-line bg-panel px-2">
        <span className="num mr-2 text-ink-3">wsignal/dev</span>
        {(['runs', 'agents', 'search'] as const).map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setRail(item)}
            className={`rounded px-2 py-1 ${
              rail === item ? 'bg-raised text-ink' : 'text-ink-3 hover:text-ink-2'
            }`}
          >
            {t(`nav.${item}`)}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            className="num rounded px-2 py-1 text-ink-3 hover:text-ink"
            onClick={() => setLanguage(i18n.language === 'ru' ? 'en' : 'ru')}
          >
            {i18n.language === 'ru' ? 'RU' : 'EN'}
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="w-60 shrink-0 overflow-y-auto border-r border-line bg-panel">
          {rail === 'runs' && <RunList selected={runId} onSelect={openRun} />}
          {rail === 'agents' && <AgentBrowser selected={agentId} onSelect={openAgent} />}
          {rail === 'search' && <TranscriptSearch runId={runId} onSelect={openAgent} />}
        </aside>

        <section className="w-80 shrink-0 overflow-y-auto border-r border-line bg-panel/60">
          <RunPanel runId={runId} selectedAgent={agentId} onSelectAgent={openAgent} />
        </section>

        <main className="min-w-0 flex-1 overflow-y-auto">
          <Transcript agentId={agentId} runId={runId} targetSeq={Number(searchParams.get('seq')) || null} />
        </main>
      </div>
    </div>
  )
}
