import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { CostsPanel } from './CostsPanel'
import App from '../App'
import type { Costs } from '../lib/api'
import { formatUsd } from '../lib/costs'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function report(overrides: Partial<Costs> = {}): Costs {
  return {
    currency: 'USD', period: { start: 1767225600, end: 1767830400 }, scope: { account_id: null, project_id: null, provider: null },
    estimated_usd: 0, known_estimated_usd: 0, billed_usd: null, known_billed_usd: 0,
    providers: [{ provider: 'openai', estimated_usd: 0, known_estimated_usd: 0, billed_usd: null, request_count: 0, unpriced_requests: 0, unmetered_jobs: 0, configuration_missing: false }],
    accounts: [], projects: [], shared_overhead_usd: 0, unassigned_project_usd: 0,
    coverage: { request_count: 0, unpriced_requests: 0, unmetered_jobs: 0, usage_days: 30, partial: false, history_partial: false, earliest_request_at: null },
    allocation: { gpu_method: 'concurrent_request_time_with_idle_shared', database_method: 'current_logical_project_bytes', snapshot_bytes: 0, project_bytes: {} },
    ...overrides,
  }
}

test('filters apply explicitly with both account and project scope and reject invalid date ranges', async () => {
  const fetch = vi.fn((_path: string) => Promise.resolve(new Response(JSON.stringify(report()))))
  vi.stubGlobal('fetch', fetch)
  render(<CostsPanel accountId="+15550000001" projectId="project one" refresh={0} onScope={vi.fn()} />)
  await screen.findByText('By provider')
  fireEvent.change(screen.getByRole('combobox', { name: 'Provider' }), { target: { value: 'nvidia' } })
  fireEvent.change(screen.getByLabelText('Start date (UTC)'), { target: { value: '2026-01-01' } })
  fireEvent.change(screen.getByLabelText('End date (UTC)'), { target: { value: '2026-01-07' } })
  expect(fetch).toHaveBeenCalledTimes(1)
  fireEvent.submit(screen.getByRole('form', { name: 'Cost filters' }))
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  const url = new URL(String(fetch.mock.calls[1][0]), 'http://localhost')
  expect(Object.fromEntries(url.searchParams)).toEqual({ start: '2026-01-01', end: '2026-01-07', account_id: '+15550000001', project_id: 'project one', provider: 'nvidia' })
  fireEvent.change(screen.getByLabelText('Start date (UTC)'), { target: { value: '2026-01-08' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Cost filters' }))
  expect(screen.getByRole('alert').textContent).toContain('end date on or after')
  expect(fetch).toHaveBeenCalledTimes(2)
  fireEvent.change(screen.getByLabelText('Start date (UTC)'), { target: { value: '2024-01-01' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Cost filters' }))
  expect(screen.getByRole('alert').textContent).toContain('366 days or fewer')
  expect(fetch).toHaveBeenCalledTimes(2)
})

test('partial estimates and small positive amounts are not presented as complete zero costs', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(report({
    estimated_usd: null, known_estimated_usd: 0.0004,
    providers: [{ provider: 'openai', estimated_usd: null, known_estimated_usd: 0.0004, billed_usd: null, request_count: 2, unpriced_requests: 1, unmetered_jobs: 0, configuration_missing: false }],
    coverage: { request_count: 2, unpriced_requests: 1, unmetered_jobs: 0, usage_days: 30, partial: true, history_partial: true, earliest_request_at: null },
  }))))))
  render(<CostsPanel refresh={0} onScope={vi.fn()} />)
  await screen.findByText('Partial known estimate: <$0.01. This is not a complete total.')
  expect(screen.getByText('Estimated cost').parentElement?.textContent).toContain('Incomplete')
  expect(screen.getByText('Estimated cost').parentElement?.textContent).not.toContain('$0.00')
  expect(screen.getByRole('heading', { name: 'Reported bill' }).parentElement?.textContent).toContain('Unavailable')
  expect(screen.getByText('This report has incomplete coverage.')).toBeTruthy()
  expect(formatUsd(0)).toBe('$0.00')
  expect(formatUsd(0.005)).toBe('<$0.01')
})

test('nonzero shared overhead stays a partial known subtotal when another infrastructure cost is missing', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(report({
    estimated_usd: null, known_estimated_usd: 3, shared_overhead_usd: 2,
    providers: [
      { provider: 'nvidia', estimated_usd: 3, known_estimated_usd: 3, billed_usd: null, request_count: 1, unpriced_requests: 0, unmetered_jobs: 0, configuration_missing: false },
      { provider: 'spacetimedb', estimated_usd: null, known_estimated_usd: 0, billed_usd: null, request_count: 0, unpriced_requests: 0, unmetered_jobs: 0, configuration_missing: true },
    ],
  }))))))
  render(<CostsPanel refresh={0} onScope={vi.fn()} />)
  const heading = await screen.findByRole('heading', { name: 'Known shared overhead' })
  expect(heading.parentElement?.textContent).toContain('$2.00')
  expect(heading.parentElement?.textContent).toContain('Partial subtotal')
  expect(screen.queryByRole('heading', { name: 'Shared overhead estimate' })).toBeNull()
})

test('changing cost scope immediately clears the previous account report', async () => {
  const fetch = vi.fn((path: string) => path.includes('account_id=first')
    ? Promise.resolve(new Response(JSON.stringify(report({ estimated_usd: 99, known_estimated_usd: 99 }))))
    : new Promise(() => {}))
  vi.stubGlobal('fetch', fetch)
  const view = render(<CostsPanel accountId="first" refresh={0} onScope={vi.fn()} />)
  await screen.findByText('$99.00')
  view.rerender(<CostsPanel accountId="second" refresh={0} onScope={vi.fn()} />)
  expect(screen.queryByText('$99.00')).toBeNull()
  expect(screen.getByRole('status').textContent).toContain('Loading')
})

test('account navigation returns to users after opening a project', async () => {
  const user = { id: '+15550000001', phone: '+15550000001', project_count: 1, conversation_count: 0, last_active: null }
  const project = { id: 'p1', name: 'Study assistant', code: 'STUDY', participant_count: 0, conversation_count: 0, last_active: null }
  const fetch = vi.fn((path: string) => {
    let data: unknown
    if (path.endsWith('/overview')) data = { users: 1, owners: 1, projects: 1, conversations: 0, updated_at: 1767225600 }
    else if (path.includes('/users?')) data = { items: [user], total: 1, offset: 0, limit: 50 }
    else if (path.endsWith('/projects/p1')) data = { project, participants: [] }
    else data = { user, projects: [project], builder_conversation: null }
    return Promise.resolve(new Response(JSON.stringify(data)))
  })
  vi.stubGlobal('fetch', fetch)
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'View account +15550000001' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Explore project' }))
  await screen.findByRole('heading', { name: 'Study assistant' })
  fireEvent.click(screen.getByRole('button', { name: 'All users' }))
  expect(await screen.findByRole('button', { name: 'View account +15550000001' })).toBeTruthy()
})
