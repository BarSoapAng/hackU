import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { useApi } from './api'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test('a previous account response cannot replace data after switching scope', async () => {
  let finishFirst!: (response: Response) => void
  const fetch = vi.fn((path: string) => path === '/first'
    ? new Promise<Response>(resolve => { finishFirst = resolve })
    : Promise.resolve(new Response(JSON.stringify({ account: 'second' }))))
  vi.stubGlobal('fetch', fetch)
  const view = renderHook(({ path }) => useApi<{ account: string }>(path), { initialProps: { path: '/first' } })
  view.rerender({ path: '/second' })
  expect(view.result.current.data).toBeUndefined()
  await waitFor(() => expect(view.result.current.data?.account).toBe('second'))
  await act(async () => finishFirst(new Response(JSON.stringify({ account: 'first' }))))
  expect(view.result.current.data?.account).toBe('second')
})

test('switching away removes the old scope before the new request completes', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => path === '/first'
    ? Promise.resolve(new Response(JSON.stringify({ account: 'first' })))
    : new Promise(() => {})))
  const view = renderHook(({ path }) => useApi<{ account: string }>(path), { initialProps: { path: '/first' } })
  await waitFor(() => expect(view.result.current.data?.account).toBe('first'))
  view.rerender({ path: '/second' })
  expect(view.result.current.data).toBeUndefined()
  expect(view.result.current.loading).toBe(true)
})

test('missing records show the safe actionable error rather than provider bodies', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('private provider error', { status: 404 }))))
  const view = renderHook(() => useApi('/missing'))
  await waitFor(() => expect(view.result.current.error).toContain('no longer available'))
  expect(view.result.current.error).not.toContain('private provider error')
})

test('retry clears a failed result and retrieves new data', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockRejectedValueOnce(new Error('secret network details'))
    .mockResolvedValueOnce(new Response(JSON.stringify({ users: 2 }))))
  const view = renderHook(() => useApi<{ users: number }>('/users'))
  await waitFor(() => expect(view.result.current.error).toContain('Check the local API'))
  act(() => view.result.current.retry())
  await waitFor(() => expect(view.result.current.data?.users).toBe(2))
  expect(view.result.current.error).toBeUndefined()
})

test('background refresh keeps same-scope data through an outage but clears deleted records', async () => {
  let finish!: (response: Response) => void
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ users: 2 })))
    .mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve }))
    .mockResolvedValueOnce(new Response('', { status: 404 }))
  vi.stubGlobal('fetch', fetch)
  const view = renderHook(({ refresh }) => useApi<{ users: number }>('/users', refresh), { initialProps: { refresh: 0 } })
  await waitFor(() => expect(view.result.current.data?.users).toBe(2))
  view.rerender({ refresh: 1 })
  expect(view.result.current.data?.users).toBe(2)
  expect(view.result.current.refreshing).toBe(true)
  await act(async () => finish(new Response('', { status: 503 })))
  expect(view.result.current.data?.users).toBe(2)
  expect(view.result.current.error).toContain('Check the local API')
  view.rerender({ refresh: 2 })
  await waitFor(() => expect(view.result.current.data).toBeUndefined())
  expect(view.result.current.error).toContain('no longer available')
})
