import { useState } from 'react'
import { ChartLegend, CorpusChart } from './Chart'
import { CLS_COLOR, GENERATED_NOTE, LOW_TRUST_TIERS, RU, SHOW_LOW_TRUST_BADGE, SHOW_SUMMARY_BADGE, date, host, num, sentences, toSeries, trend, trustedFirst } from './fmt'
import { HttpError, useCorpusSeries, useResumeRun, useStopRun, type Citation, type Entry, type EntrySeries, type Refutation } from './model'

export function Tag({ c }: { c: string | null }) {
  return <span className={`tag ${c ?? 'noise'}`}>{c ? RU.cls[c] : '—'}</span>
}

export function Forced({ e }: { e: Entry }) {
  return e.forced_reason ? <span className="tag forced" title={e.forced_reason}>не проверено</span> : null
}

export function Meter({ v, c }: { v: number | null; c: string }) {
  return <div className="meter"><i style={{ width: `${Math.round((v || 0) * 100)}%`, background: c }} /></div>
}

export function Mets({ e }: { e: Entry }) {
  const cc = CLS_COLOR[e.signal_class ?? 'noise']
  const cells: [string, number | null, string, number][] = [
    ['Слабость', e.weak_score, cc, 3],
    ['Уверенность', e.score, 'var(--tx)', 2],
    ['Р · реальность', e.substance, 'var(--b)', 2],
    ['Д · динамика', e.momentum, 'var(--v)', 2],
    ['Н · незаметность', e.faintness, 'var(--a)', 2],
  ]
  return (
    <div className="mets">
      {cells.map(([l, v, c, d], i) => (
        <div key={l}>
          <span>{l}</span>
          <b style={i === 0 ? { color: c } : undefined}>{num(v, d)}</b>
          <div className="mb"><i style={{ width: `${Math.round((v || 0) * 100)}%`, background: c }} /></div>
        </div>
      ))}
    </div>
  )
}

export function GeneratedNote() {
  return <div className="gennote"><span className="badge gen">сгенерировано моделями</span><span>{GENERATED_NOTE}</span></div>
}

export function CitationCard({ c }: { c: Citation }) {
  const foreign = c.lang && c.lang !== 'ru'
  const lowTrust = SHOW_LOW_TRUST_BADGE && LOW_TRUST_TIERS.has(c.tier)
  const generated = Boolean(SHOW_SUMMARY_BADGE && c.summary_ru && c.summary_is_generated)
  const title = foreign ? c.title_original : null
  const [quoteOpen, setQuoteOpen] = useState(false)
  return (
    <div className="cit">
      <div className="m">
        <span className={c.verified ? 'ok' : 'no'}>{c.verified ? '✓ проверена' : '✗ не найдена в тексте'}</span>
        <a href={c.url} target="_blank" rel="noopener noreferrer">{host(c.url)}</a>
        <span>{date(c.published_at)}</span>
        <span>{RU.type[c.type] || c.type}</span>
        <span>{RU.lang[c.lang] || c.lang}</span>
        <span>{RU.tier[c.tier] || c.tier}</span>
      </div>
      {title && (
        <button className={`orig tog${quoteOpen ? ' on' : ''}`} aria-expanded={quoteOpen} onClick={() => setQuoteOpen(!quoteOpen)}>{title}</button>
      )}
      <q className={title && !quoteOpen ? 'hid' : undefined}>{c.quote}</q>
      {(lowTrust || generated) && (
        <div className="flags">
          {lowTrust && <span className="badge low">пониженная доверенность</span>}
          {generated && <span className="badge gen">резюме сгенерировано автоматически</span>}
        </div>
      )}
      {c.summary_ru && (
        <div className="sum">
          {c.summary_ru}
        </div>
      )}
    </div>
  )
}

export function Evidence({ e, all = false }: { e: Entry; all?: boolean }) {
  const [open, setOpen] = useState(all)
  const ok = e.citations.filter((c) => c.verified).length
  const ordered = trustedFirst(e.citations)
  const shown = open ? ordered : ordered.slice(0, 3)
  return (
    <section className="pn">
      <h3>Доказательства <span className="x">{ok} из {e.citations.length} цитат найдены дословно</span></h3>
      {shown.map((c, i) => <CitationCard key={i} c={c} />)}
      {!open && e.citations.length > 3 && <button className="more" onClick={() => setOpen(true)}>ещё {e.citations.length - 3} источника →</button>}
    </section>
  )
}

const VERDICTS = ['proven', 'partly', 'not_proven']

export function Opponent({ r, all = false }: { r: Refutation | null | undefined; all?: boolean }) {
  const [open, setOpen] = useState<number | 'all' | null>(all ? 'all' : null)
  if (!r) {
    return (
      <section className="pn">
        <h3>Оппонент против исследователя <span className="x">нет данных</span></h3>
      </section>
    )
  }
  const counts = VERDICTS.map((v) => [v, r.claims.filter((c) => c.verdict === v).length] as const).filter(([, n]) => n)
  const shown = open === 'all' ? r.claims.map((_, i) => i) : open != null ? [open] : []
  return (
    <section className="pn">
      <h3>
        Оппонент против исследователя
        <span className="x">{r.claims.length} атак · {r.responses.length} ответов{r.rebutted && !r.responses.length ? ' (текст ответа не сохранён)' : ''}</span>
      </h3>
      <div className="vsum">{counts.map(([v, n]) => <span key={v} className={`vs-${v}`}>{n} · {RU.verdict[v]}</span>)}</div>
      <div className="pills">
        {r.claims.map((c, i) => (
          <button key={i} className={`pill ${c.verdict}${shown.includes(i) ? ' on' : ''}`} onClick={() => setOpen(open === i ? null : i)} title={c.opposite || c.claim || ''}>
            <b>{RU.verdict[c.verdict] || c.verdict}</b>{sentences(c.opposite || c.claim, 1).slice(0, 110)}
          </button>
        ))}
      </div>
      {shown.map((i) => {
        const c = r.claims[i], resp = r.responses[i]
        return (
          <div key={i} className="att">
            <span className={`v ${c.verdict}`}>{RU.verdict[c.verdict] || c.verdict}</span> {c.opposite || c.claim}
            {c.effect && <div style={{ color: 'var(--dim)', marginTop: 4 }}>{c.effect}</div>}
            {c.url && <div style={{ marginTop: 4 }}><a href={c.url} target="_blank" rel="noopener noreferrer">{host(c.url)}</a></div>}
            {resp?.response && <div className="resp">{resp.response}</div>}
            {resp?.changes && <div className="chg">изменения оценок: {resp.changes}</div>}
          </div>
        )
      })}
      {open !== 'all' && r.claims.length > 0 && <button className="more" onClick={() => setOpen('all')}>показать все атаки и ответы →</button>}
    </section>
  )
}

export function CorpusPanel({ series, error, loading }: { series: EntrySeries | undefined; error: unknown; loading: boolean }) {
  const base = series?.term ?? ''
  const [draft, setDraft] = useState<string | null>(null)
  const [custom, setCustom] = useState<string | null>(null)
  const own = useCorpusSeries(custom)
  const active = custom ? own.data : series
  const busy = custom ? own.isLoading : loading
  const err = custom ? own.error : error
  const term = custom ?? base
  const matched = active?.matched ?? active?.matched_total ?? (active ? active.months.reduce((a, m) => a + m.n, 0) : 0)
  let body
  if (busy) body = <div className="callout">загрузка ряда корпуса…</div>
  else if (err) body = <div className="callout">Корпус недоступен{err instanceof HttpError && err.status !== 404 ? `: ${err.message}` : ''}.</div>
  else if (!active || !active.months.length) body = <div className="callout">Для этой записи нет ряда корпуса.</div>
  else {
    const s = toSeries(active.months)
    const t = trend(s)
    body = (
      <>
        {active.too_broad && <div className="callout" style={{ borderColor: 'var(--a)', marginBottom: 8 }}><b>Слишком общий запрос:</b> он совпадает с большой частью корпуса, и ряд показывает не технологию. Уточните запрос.</div>}
        <CorpusChart data={s} />
        <ChartLegend />
        <div className="callout">
          {matched.toLocaleString('ru-RU')} статей за {s.length} мес. За 3 последних месяца против 6 предыдущих: доля <b>×{num(t.x)}</b>, абсолютно <b>×{num(t.rawX)}</b>. Объём корпуса растёт, поэтому сигналом считается только доля.
        </div>
      </>
    )
  }
  const src = custom ? 'ваш запрос' : series?.source === 'agent' ? 'запрос агента' : series ? 'выведен из названия' : ''
  return (
    <section className="pn">
      <h3>Корпус · помесячные упоминания <span className="x">{src}</span></h3>
      <div className="pb">
        <div className="cq">
          <input
            value={draft ?? term}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { const v = (draft ?? term).trim(); setCustom(v && v !== base ? v : null); setDraft(null) } if (e.key === 'Escape') setDraft(null) }}
            placeholder="запрос к корпусу, например: co-packaged optics OR CPO"
            aria-label="Запрос к корпусу"
          />
          {custom && <button className="btn ghost" onClick={() => { setCustom(null); setDraft(null) }}>сбросить</button>}
        </div>
        {body}
      </div>
    </section>
  )
}

export function Fold({ title, hint, children, open = false }: { title: string; hint?: string; children: React.ReactNode; open?: boolean }) {
  return (
    <details className="fold" open={open}>
      <summary>{title}{hint && <span className="x">{hint}</span>}</summary>
      <div className="body">{children}</div>
    </details>
  )
}

export function Predictors({ e }: { e: Entry }) {
  if (!e.patterns.length) return <p style={{ color: 'var(--mute)' }}>Признаки не записаны.</p>
  return (
    <p>
      {e.patterns.map((p, i) => (
        <span key={i} className="chip" style={{ cursor: 'default', display: 'inline-block', margin: '0 6px 6px 0' }}>
          {RU.kind[p.kind] || p.kind} · {p.pattern}{p.strength != null ? ` · ${p.strength}` : ''}
        </span>
      ))}
    </p>
  )
}

export function ResumeRun({ runId, state, live, spent }: { runId: number; state?: string; live: boolean; spent: number }) {
  const resume = useResumeRun()
  const [extra, setExtra] = useState(1)
  if (!state || state === 'finished' || live) return null
  const go = () => resume.mutate(
    { runId, max_cost_usd: Math.round((spent + extra) * 100) / 100 },
    { onSuccess: () => { window.location.assign(`/runs/${runId}/live`) } },
  )
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <select value={extra} onChange={(e) => setExtra(Number(e.target.value))} title="Сколько ещё можно потратить">
        <option value={0.5}>ещё $0.50</option><option value={1}>ещё $1.00</option><option value={3}>ещё $3.00</option><option value={5}>ещё $5.00</option>
      </select>
      <button className="btn" disabled={resume.isPending} onClick={go}>{resume.isPending ? 'Запуск…' : '↻ Продолжить'}</button>
      {resume.isError && <span style={{ color: 'var(--r)' }}>{resume.error.message}</span>}
    </span>
  )
}

export function StopRun({ runId, live }: { runId: number; live: boolean }) {
  const stop = useStopRun()
  if (!live) return null
  const go = () => {
    if (!window.confirm(`Остановить исследование #${runId}? Сделанное сохранится, его можно будет продолжить.`)) return
    stop.mutate(runId)
  }
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <button className="btn ghost" disabled={stop.isPending} onClick={go}>{stop.isPending ? 'Останавливаю…' : '■ Остановить'}</button>
      {stop.isError && <span style={{ color: 'var(--r)' }}>{stop.error.message}</span>}
    </span>
  )
}

