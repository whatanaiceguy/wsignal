import { monthLabel, type Point } from './fmt'

const BAR = '#3fb950'
const LINE = '#6cb6ff'
const GRID = '#1f2733'
const INK = '#9aa4b1'

export function CorpusChart({ data, w = 640, h = 210 }: { data: Point[]; w?: number; h?: number }) {
  const [pt, pr, pb, pl] = [14, 44, 26, 40]
  const W = w - pl - pr, H = h - pt - pb
  const maxB = Math.max(...data.map((d) => d.n), 1) * 1.12
  const maxL = Math.max(...data.map((d) => d.r), 1e-9) * 1.12
  const bw = W / Math.max(data.length, 1)
  const every = Math.ceil(data.length / 7)
  const ticks = [0, 1, 2, 3, 4]
  const pts = data.map((d, i) => `${(pl + i * bw + bw / 2).toFixed(1)},${(pt + H - (H * d.r) / maxL).toFixed(1)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" role="img" style={{ display: 'block', overflow: 'visible', font: '12px ui-monospace,Consolas,monospace' }}>
      {ticks.map((i) => {
        const y = pt + H - (H * i) / 4
        return (
          <g key={i}>
            <line x1={pl} x2={pl + W} y1={y} y2={y} stroke={GRID} strokeWidth={1} />
            <text x={pl - 6} y={y + 4} textAnchor="end" fill={INK}>{Math.round((maxB * i) / 4)}</text>
            <text x={pl + W + 6} y={y + 4} fill={LINE}>{((maxL * i) / 4).toFixed(maxL < 4 ? 1 : 0)}</text>
          </g>
        )
      })}
      {data.map((d, i) => {
        const bh = (H * d.n) / maxB
        const x = pl + i * bw + (bw * 0.28) / 2
        return (
          <g key={d.m}>
            <rect x={x} y={pt + H - bh} width={bw * 0.72} height={Math.max(bh, 0.5)} rx={1} fill={BAR}>
              <title>{`${monthLabel(d.m)}: ${d.n} из ${d.tot.toLocaleString('ru-RU')} статей — ${d.r.toFixed(1)} на 10 тыс.`}</title>
            </rect>
            {i % every === 0 && (
              <text x={pl + i * bw + bw / 2} y={pt + H + 17} textAnchor="middle" fill={INK}>{monthLabel(d.m)}</text>
            )}
          </g>
        )
      })}
      <polyline points={pts} fill="none" stroke={LINE} strokeWidth={2.25} strokeDasharray="5 3" strokeLinejoin="round" />
    </svg>
  )
}

export function ChartLegend() {
  return (
    <div className="legend">
      <span><i style={{ background: BAR }} />упоминаний за месяц (левая ось)</span>
      <span><i style={{ background: 'none', width: 18, height: 0, borderTop: `2px dashed ${LINE}`, verticalAlign: 3 }} />доля: на 10 тыс. статей месяца (правая ось)</span>
    </div>
  )
}

export function Spark({ data, w = 132, h = 30 }: { data: Point[]; w?: number; h?: number }) {
  if (data.length < 2) return <span style={{ color: 'var(--mute)' }}>—</span>
  const maxN = Math.max(...data.map((d) => d.n), 1)
  const maxR = Math.max(...data.map((d) => d.r), 1e-9)
  const bw = w / data.length
  const line = data.map((d, i) => `${(i * bw + bw / 2).toFixed(1)},${(h - 2 - ((h - 4) * d.r) / maxR).toFixed(1)}`).join(' ')
  const total = data.reduce((a, d) => a + d.n, 0)
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h} style={{ display: 'block' }}>
      <title>{`${total.toLocaleString('ru-RU')} статей за ${data.length} мес.: столбцы — упоминания, пунктир — доля на 10 тыс.`}</title>
      {data.map((d, i) => {
        const bh = ((h - 2) * d.n) / maxN
        return <rect key={d.m} x={i * bw + bw * 0.15} y={h - bh} width={bw * 0.7} height={Math.max(bh, 0.5)} fill={BAR} opacity={0.85} />
      })}
      <polyline points={line} fill="none" stroke={LINE} strokeWidth={1.25} strokeDasharray="3 2" />
    </svg>
  )
}
