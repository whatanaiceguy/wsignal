import { useTranslation } from 'react-i18next'
import type { Role, TurnOut } from '../types'
import { ToolArguments, ToolPayload, summarise } from './renderers'
import { Bar, RoleTag, fmt, kindBorder } from './ui'

function Meta({ turn }: { turn: TurnOut }) {
  const cached = turn.cached_tokens ?? 0
  const prompt = turn.prompt_tokens ?? 0
  const share = prompt > 0 ? `${Math.round((100 * cached) / prompt)}%` : null
  return (
    <span className="num ml-auto flex shrink-0 items-center gap-2 text-[11px] text-ink-3">
      {turn.duration_ms != null && <span>{fmt.ms(turn.duration_ms)}</span>}
      {share && <span>{share}</span>}
      {turn.cost_usd != null && turn.cost_usd > 0 && <span>{fmt.usd(turn.cost_usd)}</span>}
    </span>
  )
}

function Prose({ text }: { text: string }) {
  return <div className="mt-1 whitespace-pre-wrap text-ink">{text}</div>
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="mt-1 max-h-[32rem] overflow-auto rounded bg-sunken p-2 font-mono text-[11px] text-ink-2">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

export function TurnBlock({
  turn,
  result,
  role,
  showRole,
  maxMs,
  open,
  onToggle,
  json,
}: {
  turn: TurnOut
  result?: TurnOut | null
  role?: Role
  showRole?: boolean
  maxMs: number
  open: boolean
  onToggle: () => void
  json: boolean
}) {
  const { t } = useTranslation()
  const isTool = turn.kind === 'tool_call'
  const collapsible = isTool || turn.kind === 'system'
  const duration = result?.duration_ms ?? turn.duration_ms ?? 0
  const message = turn.content.message
  const failed = Boolean(turn.content.error ?? result?.content.payload?.error)

  return (
    <div className={`border-b border-line-soft border-l-2 py-2 pr-3 pl-3 ${kindBorder[turn.kind]}`}>
      <button
        type="button"
        onClick={onToggle}
        disabled={!collapsible}
        className="flex w-full items-center gap-2 text-left"
      >
        <span className="num w-8 shrink-0 text-ink-3">{turn.seq}</span>
        <time className="num shrink-0 text-[11px] text-ink-3" dateTime={turn.created_at} title={turn.created_at}>
          {fmt.utcClock(turn.created_at)}
        </time>
        {showRole && role && <RoleTag role={role} id={turn.agent_id} />}
        <span className="shrink-0 text-[11px] text-ink-3">
          {isTool ? turn.content.name : t(`kind.${turn.kind}`)}
        </span>
        {turn.kind === 'assistant' && turn.provider && (
          <span className="shrink-0 text-[11px] text-ink-3">{turn.provider}</span>
        )}
        {isTool && !open && (
          <span className={`truncate ${failed ? 'text-live-failed' : 'text-ink-2'}`}>
            {summarise(turn.content.name ?? '', turn.content.arguments, result?.content.payload)}
          </span>
        )}
        {duration > 0 && <Bar value={duration} max={maxMs} />}
        <Meta turn={result ?? turn} />
      </button>

      {turn.kind === 'system' && open && (
        <div className="mt-1">
          <div className="num text-[11px] text-ink-3">
            {turn.content.model} · {t('turn.prompt')} {turn.content.prompt_sha256?.slice(0, 12)}
          </div>
          <Prose text={turn.content.content ?? ''} />
        </div>
      )}
      {turn.kind === 'system' && !open && (
        <div className="num mt-0.5 pl-10 text-[11px] text-ink-3">
          {turn.content.model} · {turn.content.prompt_sha256?.slice(0, 12)}
        </div>
      )}

      {turn.kind === 'user' && <Prose text={turn.content.content ?? ''} />}

      {turn.kind === 'assistant' && (
        <div className="mt-1">
          {json ? (
            <Json value={turn.content} />
          ) : (
            <>
              {message?.reasoning && (
                <div className="mb-2 border-l border-line pl-3">
                  <div className="text-[11px] tracking-wide text-ink-3 uppercase">
                    {t('turn.reasoning')}
                  </div>
                  <div className="whitespace-pre-wrap text-ink-2 italic">{message.reasoning}</div>
                </div>
              )}
              {Boolean(message?.reasoning_details?.length) && (
                <details className="mb-2">
                  <summary className="cursor-pointer text-[11px] text-ink-3">
                    {t('turn.reasoningDetails')}
                  </summary>
                  <Json value={message?.reasoning_details} />
                </details>
              )}
              {message?.content && <Prose text={message.content} />}
              {message?.refusal && <div className="mt-1 text-live-failed">{message.refusal}</div>}
              {turn.content.error && (
                <div className="mt-1 text-live-failed">
                  {turn.content.reason}: {turn.content.error}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {isTool && open && (
        <div className="mt-2 space-y-2">
          <div className="rounded border border-line-soft bg-raised/40 p-2">
            <div className="mb-1 text-[11px] tracking-wide text-ink-3 uppercase">
              {t('turn.call')}
            </div>
            {json ? (
              <Json value={turn.content.arguments} />
            ) : (
              <ToolArguments name={turn.content.name ?? ''} args={turn.content.arguments} />
            )}
          </div>
          {result && (
            <div className="rounded border border-line-soft bg-raised/40 p-2">
              <div className="mb-1 text-[11px] tracking-wide text-ink-3 uppercase">
                {t('turn.response')}
              </div>
              {json ? (
                <Json value={result.content.payload} />
              ) : (
                <ToolPayload name={result.content.name ?? ''} payload={result.content.payload} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
