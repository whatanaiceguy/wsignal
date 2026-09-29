import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Spark } from './Chart'
import { CLS_COLOR, RU, date, dur, money, num, pct, queryLabel, sentences, toSeries } from './fmt'
import { findEntry, seriesForEntry, useCorpus, useReport, useRuns, type Entry, type EntrySeries } from './model'
import { CorpusPanel, Evidence, Fold, Forced, GeneratedNote, Meter, Mets, Opponent, Predictors, ResumeRun, StopRun, Tag } from './parts'

const isExcluded = (e: Entry) => e.signal_class === 'strong' || e.signal_class === 'noise'

function EntryPanel({ e, runId }: { e: Entry; runId: number }) {
  const text = (v: string | null | undefined) => (v ? <p>{v}</p> : null)
  return (
    <section className="pn">
      <h3>{e.name_ru} <span className="x"><Tag c={e.signal_class} /><Forced e={e} /></span></h3>
      <div className="pb">
        {!!e.edits?.length && <Fold title={`исправлено ${e.edits.length} раз`}>{e.edits.map((edit, i) => <p key={i}>{edit.reason} · {edit.changed.join(', ')}</p>)}</Fold>}
        {e.forced_reason && <div className="forced-why">Запись сделана без исследования и не проверена: {e.forced_reason}</div>}
        <div className="trans">{e.transition_ru}</div>
        <Mets e={e} />
        <p className="lead">{sentences(e.why_ru, 2)}</p>
        <Fold title={isExcluded(e) ? 'Почему исключено' : 'Почему это слабый сигнал'} hint="полное обоснование">{text(e.why_ru)}</Fold>
        <Fold title="Технология" hint="описание, проблема, преимущество, кейс">
          {e.current_state_ru && <><div className="fld"><h4>Описание и текущее состояние</h4>{text(e.current_state_ru)}</div></>}
          {e.problem_ru && <div className="fld"><h4>Какую проблему решает</h4>{text(e.problem_ru)}</div>}
          {e.advantage_ru && <div className="fld"><h4>Потенциальное преимущество</h4>{text(e.advantage_ru)}</div>}
          {e.case_example_ru && <div className="fld"><h4>Кейс-пример</h4>{text(e.case_example_ru)}</div>}
        </Fold>
        <Fold title="Динамика" hint="как меняются факты">{text(e.dynamics_ru) ?? <p style={{ color: 'var(--mute)' }}>не записано</p>}</Fold>
        <Fold title="Ключевые предикторы" hint={`${e.patterns.length} признаков`}><Predictors e={e} /></Fold>
        <Fold title="Что опровергло бы вывод">{text(e.what_would_refute_ru) ?? <p style={{ color: 'var(--mute)' }}>не записано</p>}</Fold>
        <div style={{ marginTop: 12 }}><Link className="btn ghost" to={`/runs/${runId}/e/${e.id}`}>Открыть как документ →</Link></div>
      </div>
    </section>
  )
}

const rdn = (e: Entry): [string, string, number | null, string][] => [
  ['Р', 'реальность', e.substance, 'var(--b)'],
  ['Д', 'динамика', e.momentum, 'var(--v)'],
  ['Н', 'незаметность', e.faintness, 'var(--a)'],
]

function Row({ e, series, sel, onPick }: { e: Entry; series?: EntrySeries; sel: boolean; onPick: () => void }) {
  const ok = e.citations.filter((c) => c.verified).length
  return (
    <tr className={sel ? 'sel' : ''} onClick={onPick}>
      <td className="n">{e.rank}</td>
      <td style={{ minWidth: 220 }}>{e.name_ru}<Forced e={e} /></td>
      <td><Tag c={e.signal_class} /></td>
      <td><div style={{ display: 'flex', gap: 8, alignItems: 'center' }}><Meter v={e.weak_score} c={CLS_COLOR[e.signal_class ?? 'noise']} /><span className="n">{num(e.weak_score, 3)}</span></div></td>
      <td className="n">{pct(e.score)}</td>
      <td className="hide-s">
        <div className="rdn">
          {rdn(e).map(([k, , v, c]) => <Meter key={k} v={v} c={c} />)}
          <div className="tip">
            {rdn(e).map(([k, label, v, c]) => (
              <div key={k}><i style={{ background: c }} /><b>{k}</b><span className="v">{num(v)}</span><span>{label}</span></div>
            ))}
          </div>
        </div>
      </td>
      <td className="n hide-s"><span className="ok">{ok}</span>/{e.citations.length}</td>
      <td className="hide-s">{series?.months.length ? <Spark data={toSeries(series.months)} /> : <span style={{ color: 'var(--mute)' }}>—</span>}</td>
    </tr>
  )
}

export function ReportPage() {
  const runId = Number(useParams().runId)
  const [params, setParams] = useSearchParams()
  const runs = useRuns()
  const run = runs.data?.find((r) => r.id === runId)
  const rep = useReport(runId, run?.state === 'running')
  const corpus = useCorpus(runId)
  const det = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const entries = rep.data?.entries ?? []
  const signals = entries.filter((e) => !isExcluded(e))
  const excluded = entries.filter(isExcluded)
  const sel = findEntry(entries, Number(params.get('e'))) ?? entries[0]
  const seriesFor = (e: Entry) => seriesForEntry(corpus.data, e)
  const client = useQueryClient()
  const seenCount = useRef<number | null>(null)
  useEffect(() => {
    if (seenCount.current != null && seenCount.current !== entries.length) {
      client.invalidateQueries({ queryKey: ['pub-corpus', runId] })
    }
    seenCount.current = entries.length
  }, [client, runId, entries.length])
  const runState = run?.state
  const seenState = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (seenState.current === 'running' && runState && runState !== 'running') {
      client.invalidateQueries({ queryKey: ['pub-report', runId] })
      client.invalidateQueries({ queryKey: ['pub-corpus', runId] })
    }
    seenState.current = runState
  }, [client, runId, runState])
  const pick = (e: Entry) => {
    setParams({ e: String(e.id) }, { replace: true })
    window.setTimeout(() => det.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
  }
  useEffect(() => { window.scrollTo(0, 0) }, [runId])

  if (rep.isLoading) return <div className="callout">Загрузка отчёта…</div>
  if (rep.isError || !rep.data) return <div className="callout">Отчёт не загружен: {String(rep.error?.message ?? 'нет данных')}</div>
  const r = rep.data, s = r.stats

  const topN = r.top_n && r.top_n > 0 ? r.top_n : 15
  const top = signals.slice(0, topN)
  const rest = signals.slice(topN)
  const table = (rows: Entry[], title: string, hint: string, extra?: boolean, more?: boolean) => {
    const body = (
      <>
      <div style={{ overflow: 'auto' }}>
        <table className={extra ? 'excl' : ''}>
          <thead>
            <tr><th className="n">#</th><th>Технология</th><th>Класс</th><th>Слабость</th><th className="n">Увер.</th><th className="hide-s">Р / Д / Н</th><th className="n hide-s">Цитаты</th><th className="hide-s">Корпус</th></tr>
          </thead>
          <tbody>
            {rows.map((e) => <Row key={e.id} e={e} series={seriesFor(e)} sel={sel?.id === e.id} onPick={() => pick(e)} />)}
          </tbody>
        </table>
      </div>
      {extra && rows.map((e) => (
        <div key={e.id} className="cit" style={{ cursor: 'pointer' }} onClick={() => pick(e)}>
          <div className="m"><span>#{e.rank}</span><Tag c={e.signal_class} /><span>{e.name_ru}</span></div>
          <div className="why" style={{ color: 'var(--dim)', marginTop: 4 }}>{e.signal_class === 'strong' ? 'Зрелая технология: ' : 'Не подтвердилось: '}{sentences(e.why_ru, 1)}</div>
        </div>
      ))}
      </>
    )
    if (extra || more) {
      return (
        <details className="pn" style={{ marginBottom: 12 }} open={extra && top.length === 0}>
          <summary>{title} ({rows.length}) <span className="x">{hint}</span></summary>
          {body}
        </details>
      )
    }
    return (
      <section className="pn" style={{ marginBottom: 12 }}>
        <h3>{title} <span className="x">{hint}</span></h3>
        {body}
      </section>
    )
  }

  return (
    <>
      <div className="head">
        <h1>Отчёт #{runId} · {queryLabel(r.query, run?.direction_ru)}</h1>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <StopRun runId={runId} live={!!run?.is_live} />
          <ResumeRun runId={runId} state={run?.state} live={!!run?.is_live} spent={run?.counts.cost_usd ?? 0} />
          <Link className="btn" to={`/runs/${runId}/live`}>{r.state === 'running' ? '● Ход работы' : '▶ Запись исследования'}</Link>
        </div>
      </div>
      <details className="qline">
      <summary>Поисковый запрос: <b>{r.query}</b></summary>
      <div className="runrow">
      <div className="runmeta">
        <div>{date(r.generated_at, true)} · {RU.state[r.state] || r.state}</div>
        {Object.entries(r.models_used).map(([k, v]) => <div key={k}><span className="k">{RU.role[k] || k}</span>{v}</div>)}
      </div>
      <div className="runmeta runstats">
        <div><span className="k">кандидатов найдено</span><a href="#list" onClick={(ev) => { ev.preventDefault(); list.current?.scrollIntoView({ behavior: 'smooth' }) }}>{s.candidates_found} → список</a></div>
        <div><span className="k">вероятных сигналов (≥ 0.5)</span>{s.signals_count}</div>
        <div><span className="k">с уверенностью &gt; 75%</span>{s.high_confidence_count}</div>
        <div><span className="k">цитат подтверждено</span>{s.citations_verified} / {s.citations_total}</div>
        <div><span className="k">источников обработано</span>{s.sources_processed.toLocaleString('ru-RU')}</div>
        <div><span className="k">агентов</span>{s.agents_spawned}</div>
        <div><span className="k">длительность</span>{dur(s.elapsed_seconds)}</div>
        <div><span className="k">стоимость</span>{run ? money(run.counts.cost_usd) : '—'}</div>
      </div>
      </div>
      </details>
      <GeneratedNote />
      <div ref={list} />
      {top.length > 0 && table(top, rest.length ? `Слабые сигналы · топ-${topN}` : 'Слабые сигналы', 'по слабости: реальность × динамика × незаметность')}
      {rest.length > 0 && table(rest, 'Остальные слабые сигналы', `ниже топ-${topN}, тот же порядок`, false, true)}
      {top.length === 0 && excluded.length > 0 && (
        <div className="callout">
          <b>Слабых сигналов не найдено.</b> Все кандидаты ({excluded.length}) исключены как зрелые технологии или шум; причины приведены ниже.
        </div>
      )}
      {excluded.length > 0 && table(excluded, 'Исключённые кандидаты', 'зрелые и нерелевантные, с причиной', true)}
      {!entries.length && <div className="callout">Записей пока нет.</div>}
      <div ref={det} />
      {sel && (
        <div className="det">
          <div className="col">
            <Evidence key={`ev-${sel.id}`} e={sel} />
            <Opponent key={`op-${sel.id}`} r={sel.refutation} />
          </div>
          <div className="col">
            <EntryPanel key={`en-${sel.id}`} e={sel} runId={runId} />
            <CorpusPanel key={`co-${sel.id}`} series={seriesFor(sel)} error={corpus.error} loading={corpus.isLoading} />
          </div>
        </div>
      )}
    </>
  )
}
