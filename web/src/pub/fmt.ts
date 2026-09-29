import type { Citation, Month, RunEvent } from './model'

export const RU = {
  state: { finished: 'завершено', failed: 'сбой', running: 'идёт', exhausted: 'бюджет исчерпан', cancelled: 'остановлено', interrupted: 'прервано' } as Record<string, string>,
  cls: { weak: 'слабый сигнал', strong: 'сильный сигнал', noise: 'шум' } as Record<string, string>,
  role: { orchestrator: 'оркестратор', researcher: 'исследователь', refuter: 'оппонент', assistant: 'ассистент' } as Record<string, string>,
  verdict: { proven: 'критика подтверждена', partly: 'критика подтверждена частично', not_proven: 'критика не подтверждена' } as Record<string, string>,
  type: { paper: 'статья', patent: 'патент', standard: 'стандарт', news: 'новость', report: 'отчёт', repo: 'репозиторий', social: 'соцсеть', page: 'страница', fetch_failure: 'не загружено' } as Record<string, string>,
  tier: { authoritative: 'первоисточник', trade: 'отраслевой', social: 'соцсеть', unknown: 'не определён' } as Record<string, string>,
  lang: { en: 'англ.', ru: 'рус.', de: 'нем.', fr: 'фр.', ja: 'яп.', ko: 'кор.', zh: 'кит.', es: 'исп.' } as Record<string, string>,
  kind: { substance: 'реальность', faintness: 'незаметность', delivery: 'поставка' } as Record<string, string>,
}

export const LOW_TRUST_TIERS = new Set(['social', 'unknown'])
export const SHOW_LOW_TRUST_BADGE = false
export const SHOW_SUMMARY_BADGE = true
export const GENERATED_NOTE = 'Оценки, пояснения и русские резюме источников сгенерированы моделями; цитаты приведены дословно, на языке оригинала.'

export const trustedFirst = (cs: Citation[]) =>
  [...cs].sort((a, b) => Number(LOW_TRUST_TIERS.has(a.tier)) - Number(LOW_TRUST_TIERS.has(b.tier)))

export const CLS_COLOR: Record<string, string> = { weak: 'var(--g)', strong: 'var(--a)', noise: 'var(--mute)' }

export const sentences = (t: string | null | undefined, n: number) => {
  const m = String(t || '').match(/[^.!?]+[.!?]+(\s|$)/g)
  return (m ? m.slice(0, n).join('') : String(t || '')).trim()
}

const MON = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

export const dur = (s: number | null | undefined) => {
  const t = Math.round(s || 0)
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), x = t % 60
  return h ? `${h} ч ${m} мин` : m ? `${m} мин ${String(x).padStart(2, '0')} с` : `${x} с`
}

export const clock = (s: number) => {
  const t = Math.max(0, Math.round(s))
  const h = Math.floor(t / 3600)
  const mm = String(Math.floor((t % 3600) / 60)).padStart(2, '0'), ss = String(t % 60).padStart(2, '0')
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

export const date = (iso: string | null | undefined, time = false) => {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const s = `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`
  return time ? `${s}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : s
}

export const money = (v: number | null | undefined) => '$' + (v || 0).toFixed(2)
export const stepMoney = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? '$—' : '$' + v.toFixed(v < 1 ? 4 : 2))
export const pct = (v: number | null | undefined) => (v == null ? '—' : Math.round(v * 100) + '%')
export const num = (v: number | null | undefined, d = 2) => (v == null ? '—' : Number(v).toFixed(d))
export const host = (u: string) => {
  try {
    return new URL(u).hostname.replace(/^www\./, '')
  } catch {
    return u
  }
}
export const monthLabel = (m: string) => `${MON[Number(m.slice(5, 7)) - 1]} ${m.slice(2, 4)}`

export const queryLabel = (q: string | null | undefined, direction?: string | null) => {
  const m = /seeds_(\w+)\.json/.exec(q || '')
  if (m) return `Проверка заданных технологий · набор ${m[1]}`
  return direction || q || '—'
}

export interface Point {
  m: string
  n: number
  tot: number
  r: number
}

export const toSeries = (months: Month[], last = 36): Point[] =>
  [...months]
    .sort((a, b) => a.m.localeCompare(b.m))
    .slice(-last)
    .map((x) => ({ m: x.m.slice(0, 7), n: x.n, tot: x.total, r: x.total ? (x.n / x.total) * 1e4 : 0 }))

export const trend = (data: Point[]) => {
  const avg = (a: Point[]) => (a.length ? a.reduce((s, d) => s + d.r, 0) / a.length : 0)
  const last = avg(data.slice(-3)), prev = avg(data.slice(-9, -3))
  const rawLast = data.slice(-3).reduce((s, d) => s + d.n, 0)
  const rawPrev = data.slice(-9, -3).reduce((s, d) => s + d.n, 0) / 2
  return { x: prev ? last / prev : null, rawX: rawPrev ? rawLast / rawPrev : null }
}

export type Tone = 'sys' | 'agent' | 'model' | 'tool' | 'bad' | 'good' | 'warn'

const s = (v: unknown) => (v == null ? '' : String(v))

export function eventText(e: RunEvent): { tone: Tone; text: string } | null {
  const role = RU.role[s(e.role)] || s(e.role)
  switch (e.kind) {
    case 'run_started': return { tone: 'sys', text: 'Исследование запущено' }
    case 'provider_locked': return { tone: 'sys', text: `Провайдер для ${s(e.model)}: ${s(e.provider)}` }
    case 'agent_created': return { tone: 'agent', text: `Создан агент #${s(e.agent_id)}: ${role}${e.field ? ' · ' + s(e.field) : ''}` }
    case 'model_call': {
      const tools = Array.isArray(e.tool_calls) && e.tool_calls.length ? ' → ' + (e.tool_calls as unknown[]).map(String).join(', ') : ''
      return { tone: 'model', text: `${role} #${s(e.agent_id)} · шаг ${s(e.step)} · ${num(Number(e.duration_s), 1)} с · ${stepMoney(e.cost_usd == null ? null : Number(e.cost_usd))}${tools}` }
    }
    case 'tool_done': return { tone: 'tool', text: `${role} #${s(e.agent_id)} → ${s(e.name)}${e.summary ? ': ' + s(e.summary) : ''}` }
    case 'agent_failed': return { tone: 'bad', text: `Сбой агента #${s(e.agent_id)} (${role}): ${s(e.reason) || 'ошибка'}` }
    case 'researcher_returned': return { tone: 'good', text: `Исследователь #${s(e.agent_id)} вернул «${s(e.focus)}»: ${s(e.searches)} запросов, ${s(e.sources)} источников${s(e.stopped).startsWith('failed') ? ' (по таймауту)' : ''}` }
    case 'refuter_returned': return { tone: 'warn', text: `Оппонент #${s(e.agent_id)} атаковал выводы #${s(e.attacked)}: ${s(e.searches)} запросов` }
    case 'rebuttal_returned': return { tone: 'warn', text: `Исследователь #${s(e.agent_id)} ответил на атаку` }
    case 'entry_written': return { tone: 'good', text: `Запись в отчёт: «${s(e.name)}», уверенность ${pct(Number(e.score))}, ${s(e.citations)} цитат` }
    case 'orchestrator_stopped': return { tone: 'sys', text: `Оркестратор #${s(e.agent_id)} остановлен: ${s(e.reason)}` }
    case 'run_stopped': return e.state === 'finished' ? { tone: 'good', text: 'Исследование завершено' } : { tone: 'bad', text: 'Исследование прервано, состояние сохранено' }
    case 'run_restored': return { tone: 'sys', text: `Исследование восстановлено: ${s(e.returns_replayed) || 0} результат(ов) возвращено без повтора` }
    case 'verifying': return { tone: 'sys', text: 'Проверка цитат по сохранённым текстам' }
    case 'verified': return { tone: 'good', text: `Цитаты: ${s(e.verified)} из ${s(e.checked)} найдены дословно` }
    default: return null
  }
}

export interface Step {
  s: number
  kind: string
  tone: Tone
  text: string
  e: RunEvent
  cost: number
  agents: number
  entries: number
  calls: number
  returned: number
  refuted: number
}

export function buildTimeline(events: RunEvent[]): Step[] {
  const timed = events.filter((e) => typeof e.ts === 'string')
  if (!timed.length) return []
  const t0 = new Date(timed[0].ts as string).getTime()
  let cost = 0, agents = 0, entries = 0, calls = 0, returned = 0, refuted = 0
  const out: Step[] = []
  for (const e of timed) {
    if (e.kind === 'model_call') { cost += Number(e.cost_usd) || 0; calls++ }
    if (e.kind === 'agent_created') agents++
    if (e.kind === 'entry_written') entries++
    if (e.kind === 'researcher_returned') returned++
    if (e.kind === 'refuter_returned') refuted++
    const t = eventText(e)
    if (!t) continue
    out.push({ s: (new Date(e.ts as string).getTime() - t0) / 1000, kind: e.kind, tone: t.tone, text: t.text, e, cost, agents, entries, calls, returned, refuted })
  }
  return out
}
