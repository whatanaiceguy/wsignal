import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { RU, buildTimeline, clock, date, dur, money, pct, queryLabel, type Step } from './fmt'
import { useRunEvents, useRuns } from './model'
import { ResumeRun, StopRun } from './parts'

const PHASES: [string, string][] = [['План', 'agent_created'], ['Исследование', 'researcher_returned'], ['Атака', 'refuter_returned'], ['Ответ', 'rebuttal_returned'], ['Записи', 'entry_written'], ['Проверка цитат', 'verified']]
const MARK: Record<string, [string, number]> = { agent_created: ['var(--v)', 14], researcher_returned: ['var(--g)', 22], refuter_returned: ['var(--a)', 22], rebuttal_returned: ['var(--a)', 16], entry_written: ['var(--g)', 30], agent_failed: ['var(--r)', 22], run_stopped: ['var(--r)', 30], run_restored: ['var(--b)', 22], verified: ['var(--b)', 30] }
const QUIET = new Set(['model', 'tool', 'sys'])

interface AgentView { role: string; st: string; c: string; n: number }

function derive(steps: Step[], idx: number) {
  const upto = steps.slice(0, idx)
  const agents: Record<string, AgentView> = {}
  const ents: Step['e'][] = []
  let phase = -1
  for (const x of upto) {
    const e = x.e
    const id = String(e.agent_id ?? '')
    if (e.kind === 'agent_created') agents[id] = { role: String(e.role ?? ''), st: 'работает', c: 'var(--a)', n: 0 }
    if (e.kind === 'model_call' && agents[id]) agents[id].n++
    if (e.kind === 'agent_failed' && agents[id]) Object.assign(agents[id], { st: 'сбой', c: 'var(--r)' })
    if (['researcher_returned', 'refuter_returned', 'rebuttal_returned'].includes(e.kind) && agents[id]) Object.assign(agents[id], { st: 'сдал результат', c: 'var(--g)' })
    if (e.kind === 'entry_written') ents.push(e)
    const pi = PHASES.findIndex((p) => p[1] === e.kind)
    if (pi > phase) phase = pi
  }
  return { upto, last: upto[upto.length - 1], agents, ents, phase }
}

export function RunLiveRoute() {
  const runId = Number(useParams().runId)
  return <RunLive key={runId} runId={runId} />
}

function RunLive({ runId }: { runId: number }) {
  const runs = useRuns()
  const run = runs.data?.find((r) => r.id === runId)
  const { events, state, error } = useRunEvents(runId)
  const steps = useMemo(() => buildTimeline(events), [events])
  const total = steps.length ? steps[steps.length - 1].s : 0
  const live = run?.state === 'running' || ((state === 'open' || state === 'reconnecting') && !events.some((e) => e.kind === 'end'))

  const [tRaw, setT] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [speed, setSpeed] = useState(20)
  const [follow, setFollow] = useState(true)
  const [tech, setTech] = useState(false)
  const [hover, setHover] = useState<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const drag = useRef<{ on: boolean; resume: boolean }>({ on: false, resume: false })
  const tl = useRef<HTMLDivElement>(null)
  const t = live && follow ? total : Math.min(tRaw, total)

  useEffect(() => {
    if (live && follow) return
    if (!playing || !steps.length) return
    let raf = 0
    let prev = performance.now()
    const tick = (now: number) => {
      const dt = ((now - prev) / 1000) * speed
      prev = now
      setT((v) => {
        const nv = Math.min(total, v + dt)
        if (nv >= total && !live) setPlaying(false)
        return nv
      })
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing, speed, total, live, follow, steps.length])

  let idx = 0
  while (idx < steps.length && steps[idx].s <= t + 1e-6) idx++
  const d = useMemo(() => derive(steps, idx), [steps, idx])
  const done = !live && steps.length > 0 && idx >= steps.length

  const seekTo = (v: number) => { setT(Math.max(0, Math.min(total, v))); if (live) setFollow(v >= total) }
  const leaveFollow = () => { if (live && follow) { setT(total); setFollow(false) } }
  const at = (clientX: number) => {
    const b = tl.current?.getBoundingClientRect()
    return b ? ((clientX - b.left) / Math.max(b.width, 1)) * total : 0
  }
  const onDown = (ev: React.PointerEvent) => {
    leaveFollow()
    drag.current = { on: true, resume: playing }
    setDragging(true)
    setPlaying(false)
    tl.current?.setPointerCapture(ev.pointerId)
    seekTo(at(ev.clientX))
  }
  const onMove = (ev: React.PointerEvent) => {
    const v = Math.max(0, Math.min(total, at(ev.clientX)))
    setHover(v)
    if (drag.current.on) seekTo(v)
  }
  const onUp = () => {
    if (!drag.current.on) return
    const resume = drag.current.resume
    drag.current.on = false
    setDragging(false)
    if (resume && t < total) setPlaying(true)
  }
  const toggle = () => {
    if (done) { setT(0); setPlaying(true); return }
    if (live && follow) { leaveFollow(); setPlaying(false); return }
    setPlaying((p) => !p)
  }
  const onKey = (ev: React.KeyboardEvent) => {
    const step = ev.shiftKey ? 60 : 10
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') leaveFollow()
    if (ev.key === 'ArrowRight') { ev.preventDefault(); seekTo(t + step) }
    else if (ev.key === 'ArrowLeft') { ev.preventDefault(); seekTo(t - step) }
    else if (ev.key === ' ') { ev.preventDefault(); toggle() }
  }

  const vis = d.upto.filter((x) => tech || !QUIET.has(x.tone)).slice(-150).reverse()
  const pos = total ? (t / total) * 100 : 0
  const btn = done ? '↺ сначала' : live && follow ? '❚❚ остановить' : playing ? '❚❚ пауза' : '▶ играть'

  return (
    <>
      <div className="head">
        <h1>Исследование #{runId} · {queryLabel(run?.query, run?.direction_ru)}</h1>
        <div className="meta">
          {run ? `${date(run.started_at, true)} · ${RU.state[run.state] || run.state} · ${dur(run.elapsed_s)} · ${money(run.counts.cost_usd)}` : ''}
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <StopRun runId={runId} live={!!run?.is_live} />
          <ResumeRun runId={runId} state={run?.state} live={!!run?.is_live} spent={run?.counts.cost_usd ?? 0} />
          <Link className="btn ghost" to={`/runs/${runId}`}>Отчёт</Link>
        </div>
      </div>
      <p className="lede">
        {live
          ? 'Хроника работы агентов в реальном времени. Можно перемотать назад и вернуться к текущему моменту.'
          : 'Хроника работы агентов. Это запись настоящего исследования, воспроизведённая быстрее реального времени.'}
      </p>
      {state === 'error' && <div className="callout" style={{ marginBottom: 12 }}>Поток событий прервался: {error}</div>}
      {state === 'reconnecting' && <div className="callout" style={{ marginBottom: 12 }}>Поток событий прервался ({error}), переподключаемся…</div>}
      {state !== 'loading' && !steps.length && <div className="callout" style={{ marginBottom: 12 }}>У этого исследования нет сохранённых событий.</div>}
      <div className="kpis">
        <div className="kpi"><b>{clock(t)}</b><span>{live ? 'идёт' : `из ${clock(total)}`}</span></div>
        <div className="kpi"><b>{d.last?.agents ?? 0}</b><span>агентов</span></div>
        <div className="kpi"><b>{d.last?.calls ?? 0}</b><span>вызовов моделей</span></div>
        <div className="kpi"><b>{`${d.last?.returned ?? 0} / ${d.last?.refuted ?? 0}`}</b><span>исслед. / атак</span></div>
        <div className="kpi"><b>{d.last?.entries ?? 0}</b><span>записей</span></div>
        <div className="kpi"><b>{money(d.last?.cost ?? 0)}</b><span>стоимость</span></div>
      </div>
      <div className="pl">
        <button className="btn ghost" onClick={toggle} disabled={!steps.length}>{btn}</button>
        {live && !follow && <button className="btn ghost" onClick={() => { setFollow(true); setPlaying(true) }}>● к текущему моменту</button>}
        <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
          <option value={10}>×10</option><option value={20}>×20</option><option value={60}>×60</option><option value={120}>×120</option>
        </select>
        <span>тащите шкалу, чтобы перейти к любому моменту</span>
        <label style={{ marginLeft: 'auto' }}><input type="checkbox" checked={tech} onChange={(e) => setTech(e.target.checked)} /> технические шаги</label>
      </div>
      <div
        className={`tl${dragging ? ' drag' : ''}`}
        ref={tl}
        tabIndex={0}
        role="slider"
        aria-label="Время исследования"
        aria-valuemin={0}
        aria-valuemax={Math.round(total)}
        aria-valuenow={Math.round(t)}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onPointerLeave={() => setHover(null)}
        onKeyDown={onKey}
      >
        <div className="fill" style={{ width: `${pos}%` }} />
        {total > 0 && steps.filter((x) => MARK[x.kind]).map((x, i) => {
          const m = x.kind === 'run_stopped' && x.tone === 'good' ? MARK.verified : MARK[x.kind]
          return <i key={i} className="mk" style={{ left: `${(x.s / total) * 100}%`, background: m[0], height: m[1], top: 22 - m[1] / 2 }} title={`${clock(x.s)} ${x.text}`} />
        })}
        {[0, 0.25, 0.5, 0.75, 1].map((f) => <span key={f} className="ax" style={{ left: `${f * 100}%` }}>{clock(total * f)}</span>)}
        {hover != null && total > 0 && <div className="hv" style={{ left: `${(hover / total) * 100}%`, opacity: 0.6 }} />}
        <div className="hd" style={{ left: `${pos}%` }} data-t={clock(t)} />
      </div>
      <div className="lgd">
        <span><i style={{ background: 'var(--v)' }} />агент создан</span>
        <span><i style={{ background: 'var(--g)' }} />исследователь сдал / запись</span>
        <span><i style={{ background: 'var(--a)' }} />атака и ответ</span>
        <span><i style={{ background: 'var(--r)' }} />сбой</span>
        <span><i style={{ background: 'var(--b)' }} />восстановление, проверка цитат</span>
      </div>
      <div className="phases">
        {PHASES.map((p, i) => <div key={p[0]} className={`ph${done || i < d.phase ? ' done' : ''}${!done && i === d.phase ? ' on' : ''}`}>{p[0]}</div>)}
      </div>
      <div className="grid live-grid">
        <section className="pn">
          <h3>Ход работы <span className="x">новые сверху</span></h3>
          <div className="chron">
            {state === 'loading' && <div className="post sys"><span className="tm">—</span><p>загрузка событий…</p></div>}
            {vis.map((x) => (
              <div key={`${x.e.id}-${x.s}`} className={`post ${x.tone} new`}>
                <span className="tm">{clock(x.s)}</span>
                <p>{x.text}</p>
              </div>
            ))}
          </div>
        </section>
        <section className="pn">
          <h3>Агенты</h3>
          <div className="agents">
            {Object.keys(d.agents).length ? Object.entries(d.agents).map(([id, a]) => (
              <div key={id}><span>#{id} {RU.role[a.role] || a.role}</span><i style={{ color: a.c }}>{a.st} · {a.n} выз.</i></div>
            )) : <div><i>пока никого</i></div>}
          </div>
          <h3 style={{ borderTop: '1px solid var(--line)' }}>Записи в отчёт</h3>
          <div className="agents">
            {d.ents.length ? d.ents.map((e, i) => (
              <div key={i}><span>{String(e.name ?? '')}</span><i style={{ color: 'var(--g)' }}>{pct(Number(e.score))}</i></div>
            )) : <div><i>пока нет</i></div>}
          </div>
          {done && <div className="pb"><div className="callout">Исследование завершено. <Link to={`/runs/${runId}`}>Открыть отчёт →</Link></div></div>}
        </section>
      </div>
    </>
  )
}
