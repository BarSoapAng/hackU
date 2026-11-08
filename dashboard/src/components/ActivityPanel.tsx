import { useEffect, useRef, useState } from 'react'
import { Activity as ActivityIcon, ArrowDown, ArrowUp, ChevronDown, Wrench } from 'lucide-react'
import { accountPath, formatTime, useApi, type Activity, type ModelRequest } from '../lib/api'
import { formatUsd } from '../lib/costs'
import { Badge, Button, Card, Empty, RequestState, cn } from './ui'

export function ActivityPanel({ accountId, conversationId, refresh, turnId, onClearTurn }: {
  accountId: string; conversationId: string; refresh: number; turnId?: string | null; onClearTurn?: () => void
}) {
  const [offset, setOffset] = useState(0)
  const heading = useRef<HTMLHeadingElement>(null)
  const request = useApi<Activity>(`${accountPath(accountId)}/conversations/${encodeURIComponent(conversationId)}/activity?offset=${turnId ? 0 : offset}&limit=25${turnId ? `&turn_id=${encodeURIComponent(turnId)}` : ''}`, refresh)
  useEffect(() => { if (turnId) heading.current?.focus() }, [turnId])
  return <Card className="min-w-0 overflow-hidden">
    <div className="border-b p-5"><h2 ref={heading} tabIndex={-1} className="flex items-center gap-2 rounded text-lg font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><ActivityIcon className="size-5 text-primary" aria-hidden="true" />Sprout activity</h2><p className="mt-1 text-sm text-muted-foreground">{turnId ? 'Activity for the selected message’s turn' : 'Requests and tool outcomes, newest turns first'}</p>{turnId && <Button variant="outline" className="mt-3" onClick={() => { setOffset(0); onClearTurn?.() }}>Show all turns</Button>}</div>
    <RequestState {...request} />
    {request.data && <>
      <div className="border-b bg-muted/40 px-5 py-3 text-sm leading-relaxed text-muted-foreground">Tool details are retained for {request.data.coverage.trace_days} days; recorded request usage for {request.data.coverage.usage_days} days.{request.refreshing && <span role="status" className="ml-1">Refreshing…</span>}{request.error && <p className="mt-1 font-medium">Showing previously loaded activity.</p>}</div>
      {!turnId && (request.data.total > 25 || offset > 0) && <nav aria-label="Activity pages" className="flex flex-wrap items-center justify-between gap-2 border-b px-5 py-3"><Button variant="outline" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 25))}><ArrowUp />Newer turns</Button><Button variant="outline" disabled={offset + request.data.items.length >= request.data.total} onClick={() => setOffset(value => value + 25)}>Older turns<ArrowDown /></Button></nav>}
      {!request.data.items.length && <Empty title={turnId ? 'No retained activity for this turn' : offset ? 'No turns on this page' : 'No recorded activity'}>{turnId ? 'This message’s activity may have expired or was not recorded. Its transcript can remain available longer.' : offset ? <Button variant="outline" onClick={() => setOffset(0)}>Go to latest turns</Button> : 'Activity appears when a message is processed. Older turns may no longer be retained.'}</Empty>}
      <div className="divide-y">
        {request.data.items.map(turn => <details key={turn.id} open={turnId === turn.id || undefined} className="group/turn">
          <summary className="flex min-h-20 cursor-pointer list-none flex-wrap items-center justify-between gap-3 px-5 py-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring [&::-webkit-details-marker]:hidden"><span><span className="block font-medium">{formatTime(turn.created)}</span><span className="mt-1 block text-sm text-muted-foreground">{turn.requests.length} requests · {turn.tools.length} tools</span></span><span className="flex items-center gap-2"><Status status={turn.status} /><ChevronDown className="size-4 transition-transform group-open/turn:rotate-180" aria-hidden="true" /></span></summary>
          <div className="space-y-5 border-t bg-muted/20 p-5">
            <p className="text-sm text-muted-foreground">{turn.attempts} processing attempts · {turn.generations} model generations</p>
            {!turn.trace_available && <p className="text-sm leading-relaxed text-muted-foreground">Detailed tool arguments and results are unavailable for this turn.</p>}
            {turn.tools.map(tool => <details key={tool.id} className="rounded-lg border bg-card">
              <summary className="flex cursor-pointer flex-wrap items-center justify-between gap-2 p-4 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><span className="flex min-w-0 items-center gap-2 break-all"><Wrench className="size-4 shrink-0 text-primary" aria-hidden="true" />{tool.name}</span><Status status={tool.status} /></summary>
              <div className="space-y-4 border-t p-4"><p className="text-sm text-muted-foreground">{tool.created === null ? 'Time not recorded' : formatTime(tool.created)}</p><Payload label="Arguments" value={tool.arguments} /><Payload label="Result" value={tool.result} /></div>
            </details>)}
            {turn.requests.map((item, index) => <RequestCard key={item.id} request={item} sequence={item.sequence ?? index + 1} />)}
            {!turn.usage_recorded && <p className="rounded-lg border bg-card p-4 text-sm leading-relaxed text-muted-foreground">Request usage was not recorded for this turn. Missing values are not zero usage.</p>}
          </div>
        </details>)}
      </div>
    </>}
  </Card>
}

function Status({ status }: { status: string }) {
  return <Badge className={cn('capitalize', ['failed', 'error'].includes(status) && 'bg-red-50 text-destructive', ['completed', 'received'].includes(status) && 'bg-accent text-primary')}>{status === 'received' ? 'Response received' : status.replaceAll('_', ' ')}</Badge>
}

function Payload({ label, value }: { label: string; value: Record<string, unknown> | null }) {
  return <div><h3 className="mb-2 text-sm font-medium">{label}</h3>{value === null ? <p className="text-sm text-muted-foreground">Not recorded or no longer retained</p> : <pre className="max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-muted p-3 text-sm leading-relaxed [overflow-wrap:anywhere]">{JSON.stringify(value, null, 2)}</pre>}</div>
}

function RequestCard({ request, sequence }: { request: ModelRequest; sequence: number }) {
  return <div className="space-y-4 rounded-lg border bg-card p-4">
    <div className="flex flex-wrap items-start justify-between gap-2"><div><p className="text-sm font-semibold capitalize">{request.operation?.replaceAll('_', ' ') ?? 'Model request'} · Request {sequence}</p><p className="mt-1 break-all text-sm text-muted-foreground">{request.provider === 'nvidia' ? 'NVIDIA' : request.provider === 'openai' ? 'OpenAI' : 'Provider not recorded'} · {request.model ?? 'Model not recorded'}</p></div><Status status={request.status} /></div>
    <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">{[
      ['Input tokens', request.input_tokens?.toLocaleString() ?? 'Not recorded'],
      ['Output tokens', request.output_tokens?.toLocaleString() ?? 'Not recorded'],
      ['Cached input', request.cached_input_tokens?.toLocaleString() ?? 'Not recorded'],
      ['Total tokens', request.total_tokens?.toLocaleString() ?? 'Not recorded'],
      ['Duration', request.duration_ms === null ? 'Not recorded' : request.duration_ms < 1000 ? `${Math.round(request.duration_ms)} ms` : `${(request.duration_ms / 1000).toFixed(2)} s`],
      ['Started', request.started === null ? 'Not recorded' : formatTime(request.started)],
    ].map(([label, value]) => <div key={label}><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 break-words font-medium tabular-nums">{value}</dd></div>)}</dl>
    {request.status === 'received' && <p className="text-sm leading-relaxed text-muted-foreground">Provider response received. The turn and message statuses show processing and delivery outcomes.</p>}
    {(request.error_type || request.http_status) && <p className="break-words text-sm text-destructive">{request.error_type ?? 'Provider error'}{request.http_status ? ` · HTTP ${request.http_status}` : ''}</p>}
    {request.status === 'unknown' && <p className="text-sm text-muted-foreground">The request outcome could not be confirmed.</p>}
    {request.cost && <div className="border-t pt-3 text-sm"><p className="font-medium">{request.cost.basis === 'shared_gpu' ? 'Cost basis: shared GPU runtime' : request.cost.estimated_usd !== null ? `Estimated API cost · ${formatUsd(request.cost.estimated_usd)}` : request.cost.basis === 'usage_unknown' ? 'Estimated cost · Usage not recorded' : request.cost.basis === 'configuration_error' ? 'Estimated cost · Configuration needs attention' : 'Estimated cost · Rate not configured'}</p>{request.cost.basis === 'shared_gpu' && <p className="mt-1 leading-relaxed text-muted-foreground">See configured runtime and coverage in Costs.</p>}</div>}
  </div>
}
