import { useCallback, useEffect, useState } from 'react'

export type User = {
  id: string; phone: string; project_count: number; conversation_count: number; last_active: number | null
}
export type Project = {
  id: string; name: string; code: string; participant_count: number; conversation_count: number; last_active: number | null
}
export type Page<T> = { items: T[]; total: number; offset: number; limit: number }
export type Overview = { users: number; owners: number; projects: number; conversations: number; updated_at: number }
export type Account = {
  user: User; projects: Project[];
  builder_conversation: { id: string; participant_id: string; last_active: number | null; message_count: number } | null
}
export type Participant = { id: string; phone: string; conversation_id: string; message_count: number; last_active: number | null }
export type ProjectDetail = { project: Project; participants: Participant[] }
export type Message = { role: string; content: string; created: number; job_id: string | null; delivery_status: string | null }
export type Conversation = {
  id: string; kind: 'builder' | 'project'; project_id: string | null; participant_id: string;
  messages: Message[]; total: number; offset: number; limit: number;
  coverage: { message_days: number; message_limit: number; trace_days: number }
}
export type ModelRequest = {
  id: string; sequence: number | null; provider: 'nvidia' | 'openai' | null; operation: 'builder' | 'answer' | 'query_embedding' | 'document_embedding' | null;
  model: string | null; started: number | null; finished: number | null; duration_ms: number | null;
  status: 'pending' | 'received' | 'failed' | 'unknown'; input_tokens: number | null; output_tokens: number | null;
  cached_input_tokens: number | null; total_tokens: number | null; provider_status: string | null; error_type: string | null; http_status: number | null
  cost?: { estimated_usd: number | null; basis: 'token_estimate' | 'shared_gpu' | 'not_configured' | 'usage_unknown' | 'configuration_error' }
}
export type ActivityTurn = {
  id: string; created: number; status: string; attempts: number; generations: number; trace_available: boolean;
  tools: { id: string; name: string; created: number | null; arguments: Record<string, unknown> | null; result: Record<string, unknown> | null; status: string }[];
  requests: ModelRequest[]; usage_recorded: boolean
}
export type Activity = Page<ActivityTurn> & { coverage: { trace_days: number; usage_days: number } }
export type CostBreakdown = {
  id: string; label: string; estimated_usd: number | null; known_estimated_usd: number;
  providers: Record<string, number>; request_count: number; unpriced_requests: number;
  account_id?: string | null; deleted?: boolean
}
export type Costs = {
  currency: 'USD'; period: { start: number; end: number };
  scope: { account_id: string | null; project_id: string | null; provider: string | null };
  estimated_usd: number | null; known_estimated_usd: number; billed_usd: number | null; known_billed_usd: number;
  providers: { provider: string; estimated_usd: number | null; known_estimated_usd: number; billed_usd: number | null;
    request_count: number; unpriced_requests: number; unmetered_jobs: number; configuration_missing: boolean }[];
  accounts: CostBreakdown[]; projects: CostBreakdown[]; shared_overhead_usd: number; unassigned_project_usd: number;
  coverage: { request_count: number; unpriced_requests: number; unmetered_jobs: number; usage_days: number;
    partial: boolean; history_partial: boolean; earliest_request_at: number | null };
  allocation: { gpu_method: string; database_method: string; snapshot_bytes: number; project_bytes: Record<string, number> }
}

export function accountPath(id: string) {
  return `/api/internal/accounts/${encodeURIComponent(id)}`
}

class DashboardError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

export function useApi<T>(path: string | null, refresh = 0) {
  const [result, setResult] = useState<{ path: string | null; refresh: number; data?: T; error?: string; pending?: boolean }>({ path: null, refresh: 0 })
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    setResult(previous => ({ path, refresh, data: previous.path === path ? previous.data : undefined, pending: true }))
    fetch(path, { signal: controller.signal, cache: 'no-store' })
      .then(async response => {
        if (!response.ok) throw new DashboardError(response.status === 404
          ? 'This record is no longer available. Refresh or choose another user.'
          : 'The dashboard could not load this data. Check the local API and try again.', response.status)
        return response.json() as Promise<T>
      })
      .then(data => { if (!controller.signal.aborted) setResult({ path, refresh, data }) })
      .catch(error => {
        if (!controller.signal.aborted) setResult(previous => ({
          path, refresh,
          data: previous.path === path && !(error instanceof DashboardError && error.status === 404) ? previous.data : undefined,
          error: error instanceof DashboardError ? error.message : 'The dashboard could not load this data. Check the local API and try again.',
        }))
      })
    return () => controller.abort()
  }, [path, refresh, retry])
  const current = result.path === path ? result : undefined
  return { data: current?.data, error: current?.error, loading: !!path && !current?.data && !current?.error,
    refreshing: !!current?.data && (!!current.pending || current.refresh !== refresh),
    retry: useCallback(() => setRetry(value => value + 1), []) }
}

export function formatTime(seconds: number | null) {
  return seconds ? new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'No activity yet'
}
