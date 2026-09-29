import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RU, date, dur, queryLabel } from './fmt'
import { useRuns, useStartRun } from './model'

const EXAMPLES = ['Оптические интерконнекты для ИИ-дата-центров', 'Постквантовая криптография в банках', 'Натрий-ионные накопители для сетей', 'Безопасность ИИ-агентов']

export function StartPage() {
  const runs = useRuns()
  const start = useStartRun()
  const nav = useNavigate()
  const [q, setQ] = useState('')
  const [depth, setDepth] = useState(7)
  const [budget, setBudget] = useState(3)
  const [top, setTop] = useState<number | null>(15)
  const list = runs.data ?? []
  const submit = () => {
    if (!q.trim() || start.isPending) return
    start.mutate(
      { query: q.trim(), max_research: depth, max_cost_usd: budget, top: top ?? depth },
      { onSuccess: (r) => nav(`/runs/${r.run_id}/live`) },
    )
  }
  return (
    <div className="grid g2 swap">
      <section className="pn">
        <h3>
          История исследований <span className="x">{runs.isLoading ? 'загрузка…' : `${list.length} записей`}</span>
        </h3>
        {runs.isError && <div className="pb"><div className="callout">Не удалось загрузить историю: {String(runs.error?.message)}</div></div>}
        <div style={{ overflow: 'auto', maxHeight: 620 }}>
          <table>
            <thead>
              <tr>
                <th>#</th><th>Запрос</th><th>Состояние</th><th className="hide-s">Начато</th><th className="n hide-s">Длит.</th><th className="n">Зап.</th><th className="n hide-s">Цит.</th><th className="n hide-s">$</th>
              </tr>
            </thead>
            <tbody>
              {list.map((r) => (
                <tr key={r.id} onClick={() => nav(r.state === 'running' ? `/runs/${r.id}/live` : `/runs/${r.id}`)}>
                  <td className="n">{r.id}</td>
                  <td>{queryLabel(r.query, r.direction_ru)}</td>
                  <td><span className={`st ${r.state}`}>{RU.state[r.state] || r.state}</span></td>
                  <td className="hide-s" style={{ color: 'var(--dim)', whiteSpace: 'nowrap' }}>{date(r.started_at, true)}</td>
                  <td className="n hide-s">{dur(r.elapsed_s)}</td>
                  <td className="n">{r.counts.entries}</td>
                  <td className="n hide-s">{r.counts.citations}</td>
                  <td className="n hide-s">{r.counts.cost_usd.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="pn">
        <h3>Новое исследование</h3>
        <div className="pb">
          <textarea
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit() }}
            placeholder="Направление или технология, например: оптика в одном корпусе с ускорителем"
          />
          <div className="chips">
            {EXAMPLES.map((x) => <span key={x} className="chip" onClick={() => setQ(x)}>{x}</span>)}
          </div>
          <div className="params">
            <label>Глубина
              <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
                <option value={4}>до 4 технологий</option><option value={7}>до 7</option><option value={15}>до 15</option><option value={30}>до 30</option>
              </select>
            </label>
            <label>Бюджет
              <select value={budget} onChange={(e) => setBudget(Number(e.target.value))}>
                <option value={1}>$1.00</option><option value={3}>$3.00</option><option value={5}>$5.00</option>
              </select>
            </label>
            <label>В отчёте
              <select value={top ?? 0} onChange={(e) => setTop(Number(e.target.value) || null)}>
                <option value={15}>ТОП-15</option><option value={0}>все</option>
              </select>
            </label>
          </div>
          <button className="btn" disabled={!q.trim() || start.isPending} onClick={submit}>
            {start.isPending ? 'Запуск…' : '▶ Запустить исследование'}
          </button>
          {start.isError && <div className="callout" style={{ borderColor: 'var(--r)' }}><b style={{ color: 'var(--r)' }}>Не запущено.</b> {start.error.message}</div>}
          <div className="note">Исследование занимает 10–40 минут: оркестратор делит направление, исследователи собирают датированные доказательства, оппоненты пытаются их опровергнуть, цитаты проверяются дословно. Можно закрыть вкладку: исследование продолжится и появится в истории. Ctrl+Enter запускает.</div>
        </div>
      </section>
    </div>
  )
}
