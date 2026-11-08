import { useState } from 'react'
import { ArrowDown, ArrowUp, MessageSquare, RefreshCw } from 'lucide-react'
import { accountPath, formatTime, useApi, type Conversation } from '../lib/api'
import { Badge, Button, Card, Empty, RequestState, cn } from './ui'

export function ConversationPanel({ accountId, conversationId, refresh, controls = true, selectedTurnId, onSelectTurn }: {
  accountId: string; conversationId: string; refresh: number; controls?: boolean; selectedTurnId?: string | null; onSelectTurn?: (id: string) => void
}) {
  const [offset, setOffset] = useState(0)
  const [localRefresh, setLocalRefresh] = useState(0)
  const request = useApi<Conversation>(`${accountPath(accountId)}/conversations/${encodeURIComponent(conversationId)}?offset=${offset}&limit=50`, refresh + localRefresh)
  return <Card className="min-w-0 overflow-hidden">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b p-5">
      <div className="min-w-0"><h2 className="flex items-center gap-2 text-lg font-semibold"><MessageSquare className="size-5 shrink-0 text-primary" aria-hidden="true" />{request.data?.kind === 'builder' ? 'Builder conversation' : 'Conversation'}</h2>{request.data && <p className="mt-1 break-all font-medium">With {request.data.participant_id}</p>}<p className="mt-1 text-sm text-muted-foreground">{request.data ? `${request.data.total.toLocaleString()} retained messages` : 'Message transcript'}</p></div>
      {controls && <Button variant="ghost" onClick={() => setLocalRefresh(value => value + 1)} aria-label="Refresh conversation"><RefreshCw />Refresh</Button>}
    </div>
    <RequestState {...request} />
    {request.data && <>
      <div className="border-b bg-muted/40 px-5 py-3 text-sm leading-relaxed text-muted-foreground">History covers up to {request.data.coverage.message_limit} messages from the last {request.data.coverage.message_days} days. Message times use your local timezone.{request.refreshing && <span role="status" className="ml-1">Refreshing…</span>}{request.error && <p className="mt-1 font-medium">Showing previously loaded messages.</p>}</div>
      {(request.data.total > 50 || offset > 0) && <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3"><Button variant="outline" disabled={offset + request.data.messages.length >= request.data.total} onClick={() => setOffset(value => value + 50)}><ArrowUp />Earlier messages</Button><span className="text-sm text-muted-foreground">{offset === 0 ? 'Latest messages' : 'Earlier messages'}</span><Button variant="outline" disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 50))}>Newer messages<ArrowDown /></Button></div>}
      {!request.data.messages.length && <Empty title={offset ? 'No messages on this page' : 'No retained messages'}>{offset ? <><p>Older messages may have expired from the retained history.</p><Button variant="outline" className="mt-4" onClick={() => setOffset(0)}>Go to latest messages</Button></> : 'New messages will appear here after the conversation runs.'}</Empty>}
      <ol className="space-y-5 p-5 sm:p-6" aria-label="Message transcript">
        {request.data.messages.map((message, index) => <li key={`${offset}:${index}`} className={cn('max-w-3xl rounded-xl border p-4 sm:p-5', message.role === 'assistant' ? 'mr-4 bg-card sm:mr-12' : 'ml-4 border-transparent bg-muted sm:ml-12')}>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm"><span className="font-semibold">{message.role === 'assistant' ? 'Agent' : message.role === 'user' ? 'Participant' : message.role}</span><time dateTime={new Date(message.created * 1000).toISOString()} className="text-muted-foreground">{formatTime(message.created)}</time></div>
          <p className="whitespace-pre-wrap break-words leading-relaxed [overflow-wrap:anywhere]">{message.content}</p>
          {message.role === 'assistant' && message.delivery_status && <div className="mt-3"><Badge className={cn(message.delivery_status === 'failed' && 'bg-red-50 text-destructive')}>{message.delivery_status.replaceAll('_', ' ')}</Badge></div>}
          {message.job_id && onSelectTurn && <Button variant="ghost" className="mt-3 px-2" aria-pressed={selectedTurnId === message.job_id} onClick={() => onSelectTurn(message.job_id!)}>{selectedTurnId === message.job_id ? 'Showing this turn’s activity' : 'View activity'}</Button>}
        </li>)}
      </ol>
    </>}
  </Card>
}
