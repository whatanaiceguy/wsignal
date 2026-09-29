import { Link, useParams } from 'react-router-dom'
import { RU, date, dur, queryLabel, trustedFirst } from './fmt'
import { findEntry, seriesForEntry, useCorpus, useReport, useRuns } from './model'
import { CitationCard, CorpusPanel, Fold, Forced, GeneratedNote, Mets, Opponent, Predictors, Tag } from './parts'

export function EntryDoc() {
  const p = useParams()
  const runId = Number(p.runId), ref = Number(p.entry)
  const rep = useReport(runId)
  const corpus = useCorpus(runId)
  const run = useRuns().data?.find((r) => r.id === runId)
  const e = findEntry(rep.data?.entries ?? [], ref)
  if (rep.isLoading) return <div className="callout">Загрузка…</div>
  if (!rep.data || !e) return <div className="callout">Запись не найдена.</div>
  const sec = (h: string, v: string | null | undefined) => (v ? <><h2>{h}</h2><p>{v}</p></> : null)
  const excluded = e.signal_class === 'strong' || e.signal_class === 'noise'
  return (
    <article className="doc">
      <div className="noprint" style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
        <Link className="btn ghost" to={`/runs/${runId}?e=${e.id}`}>← к отчёту</Link>
        <button className="btn ghost" onClick={() => window.print()}>Печать / PDF</button>
      </div>
      <div className="meta" style={{ color: 'var(--dim)', font: '13px var(--mono)' }}>
        Отчёт #{runId} · запись {e.rank} · {date(rep.data.generated_at)} · {queryLabel(rep.data.query, run?.direction_ru)}
      </div>
      <h1>{e.name_ru}</h1>
      <GeneratedNote />
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 14, color: 'var(--dim)' }}>
        <Tag c={e.signal_class} /><Forced e={e} />{e.name_en && <span>{e.name_en}</span>}<span>· {e.searches_run} запросов, {e.sources_checked} площадок</span>
      </div>
      {!!e.edits?.length && <Fold title={`исправлено ${e.edits.length} раз`}>{e.edits.map((edit, i) => <p key={i}>{edit.reason} · {edit.changed.join(', ')}</p>)}</Fold>}
      {e.forced_reason && <div className="forced-why">Запись сделана без исследования и не проверена: {e.forced_reason}</div>}
      <div className="trans">{e.transition_ru}</div>
      <Mets e={e} />
      {sec('Описание технологии и текущее состояние', e.current_state_ru)}
      {sec('Какую проблему решает', e.problem_ru)}
      {sec('Потенциальное преимущество', e.advantage_ru)}
      {sec('Кейс-пример', e.case_example_ru)}
      {sec(excluded ? 'Почему исключено из слабых сигналов' : 'Почему это слабый сигнал', e.why_ru)}
      {sec('Динамика', e.dynamics_ru)}
      <h2>Ключевые предикторы и причины уверенности</h2>
      <p>Слабость {e.weak_score?.toFixed(3) ?? '—'} собрана из трёх оценок: реальность {e.substance ?? '—'}, динамика {e.momentum ?? '—'}, незаметность {e.faintness ?? '—'}. Уверенность модели {Math.round(e.score * 100)}%. Из {e.citations.length} цитат {e.citations.filter((c) => c.verified).length} найдены дословно в источниках.</p>
      <Predictors e={e} />
      <h2>Корпус</h2>
      <CorpusPanel series={seriesForEntry(corpus.data, e)} error={corpus.error} loading={corpus.isLoading} />
      <h2>Проверка оппонентом</h2>
      <Opponent r={e.refutation} all />
      {sec('Что опровергло бы вывод', e.what_would_refute_ru)}
      <h2>Источники и аналитические материалы · {e.citations.length}</h2>
      <section className="pn">{trustedFirst(e.citations).map((c, i) => <CitationCard key={i} c={c} />)}</section>
      <p className="note" style={{ marginTop: 16 }}>Исследование длилось {dur(rep.data.stats.elapsed_seconds)}; модели: {Object.entries(rep.data.models_used).map(([k, v]) => `${RU.role[k] || k} ${v}`).join(', ')}.</p>
    </article>
  )
}
