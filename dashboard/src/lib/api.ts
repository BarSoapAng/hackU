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

export function accountPath(id: string) {
  return `/api/internal/accounts/${encodeURIComponent(id)}`
}

class DashboardError extends Error {}

export function useApi<T>(path: string | null, refresh = 0) {
  const [result, setResult] = useState<{ path: string | null; refresh: number; data?: T; error?: string }>({ path: null, refresh: 0 })
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    setResult({ path, refresh })
    fetch(path, { signal: controller.signal, cache: 'no-store' })
      .then(async response => {
        if (!response.ok) throw new DashboardError(response.status === 404
          ? 'This record is no longer available. Refresh or choose another user.'
          : 'The dashboard could not load this data. Check the local API and try again.')
        return response.json() as Promise<T>
      })
      .then(data => { if (!controller.signal.aborted) setResult({ path, refresh, data }) })
      .catch(error => {
        if (!controller.signal.aborted) setResult({ path, refresh, error: error instanceof DashboardError ? error.message : 'The dashboard could not load this data. Check the local API and try again.' })
      })
    return () => controller.abort()
  }, [path, refresh, retry])
  const current = result.path === path && result.refresh === refresh ? result : undefined
  return { data: current?.data, error: current?.error, loading: !!path && !current?.data && !current?.error, retry: useCallback(() => setRetry(value => value + 1), []) }
}

export function formatTime(seconds: number | null) {
  return seconds ? new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'No activity yet'
}
