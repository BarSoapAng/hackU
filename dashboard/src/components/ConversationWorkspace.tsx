import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { ActivityPanel } from './ActivityPanel'
import { ConversationPanel } from './ConversationPanel'
import { Button } from './ui'

export function ConversationWorkspace({ accountId, conversationId, refresh }: { accountId: string; conversationId: string; refresh: number }) {
  const [live, setLive] = useState(false)
  const [updates, setUpdates] = useState(0)
  const [turnId, setTurnId] = useState<string | null>(null)
  useEffect(() => {
    if (!live) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') setUpdates(value => value + 1)
    }, 15000)
    return () => window.clearInterval(timer)
  }, [live])
  return <section className="@container min-w-0 space-y-4" aria-label="Conversation and agent activity">
    <div className="flex flex-wrap items-center justify-end gap-4"><label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm"><input type="checkbox" checked={live} onChange={event => setLive(event.target.checked)} className="size-4 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" />Live updates · every 15 seconds</label><Button variant="outline" onClick={() => setUpdates(value => value + 1)} aria-label="Refresh conversation and activity"><RefreshCw />Refresh both</Button></div>
    <div className="grid items-start gap-5 @4xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]"><ConversationPanel accountId={accountId} conversationId={conversationId} refresh={refresh + updates} controls={false} selectedTurnId={turnId} onSelectTurn={setTurnId} /><ActivityPanel accountId={accountId} conversationId={conversationId} refresh={refresh + updates} turnId={turnId} onClearTurn={() => setTurnId(null)} /></div>
  </section>
}
