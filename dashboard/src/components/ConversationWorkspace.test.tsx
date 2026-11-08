import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { ConversationWorkspace } from './ConversationWorkspace'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

test('live updates refresh transcript and activity only while the page is visible', async () => {
  vi.useFakeTimers()
  const visible = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const fetch = vi.fn((path: string) => Promise.resolve(new Response(JSON.stringify(path.includes('/activity')
    ? { items: [], total: 0, offset: 0, limit: 25, coverage: { trace_days: 7, usage_days: 30 } }
    : { id: 'thread', kind: 'builder', participant_id: 'owner', project_id: null, messages: [], total: 0, offset: 0, limit: 50, coverage: { message_days: 30, message_limit: 200, trace_days: 7 } }))))
  vi.stubGlobal('fetch', fetch)
  await act(async () => { render(<ConversationWorkspace accountId="owner" conversationId="thread" refresh={0} />) })
  expect(fetch).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Live updates · every 15 seconds' }))
  await act(async () => { vi.advanceTimersByTime(15000) })
  expect(fetch).toHaveBeenCalledTimes(4)
  visible.mockReturnValue('hidden')
  await act(async () => { vi.advanceTimersByTime(30000) })
  expect(fetch).toHaveBeenCalledTimes(4)
  visible.mockReturnValue('visible')
  await act(async () => { vi.advanceTimersByTime(15000) })
  expect(fetch).toHaveBeenCalledTimes(6)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Live updates · every 15 seconds' }))
  await act(async () => { vi.advanceTimersByTime(15000) })
  expect(fetch).toHaveBeenCalledTimes(6)
})

test('a message opens its specific activity turn beyond the latest page and can return to all turns', async () => {
  const fetch = vi.fn((path: string) => {
    const url = new URL(path, 'http://localhost')
    const selected = url.searchParams.get('turn_id')
    return Promise.resolve(new Response(JSON.stringify(path.includes('/activity')
      ? { items: selected ? [{ id: selected, created: 1767225600, status: 'completed', attempts: 1, generations: 0, trace_available: false, tools: [], requests: [], usage_recorded: false }] : [], total: selected ? 1 : 0, offset: 0, limit: 25, coverage: { trace_days: 7, usage_days: 30 } }
      : { id: 'thread', kind: 'builder', participant_id: 'owner', project_id: null, messages: [{ role: 'user', content: 'An older message', created: 1767225600, job_id: 'older:turn', delivery_status: null }], total: 1, offset: 0, limit: 50, coverage: { message_days: 30, message_limit: 200, trace_days: 7 } })))
  })
  vi.stubGlobal('fetch', fetch)
  render(<ConversationWorkspace accountId="owner" conversationId="thread" refresh={0} />)
  fireEvent.click(await screen.findByRole('button', { name: 'View activity' }))
  await screen.findByText('0 requests · 0 tools')
  expect(fetch.mock.calls.some(([path]) => path.includes('turn_id=older%3Aturn'))).toBe(true)
  expect(screen.getByText('0 requests · 0 tools').closest('details')?.open).toBe(true)
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Sprout activity' }))
  expect(screen.getByRole('button', { name: 'Showing this turn’s activity' }).getAttribute('aria-pressed')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: 'Show all turns' }))
  await waitFor(() => expect(screen.queryByText('0 requests · 0 tools')).toBeNull())
  expect(screen.getByRole('button', { name: 'View activity' }).getAttribute('aria-pressed')).toBe('false')
})
