import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { ActivityPanel } from './ActivityPanel'
import type { ActivityTurn } from '../lib/api'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function turn(overrides: Partial<ActivityTurn> = {}): ActivityTurn {
  return {
    id: 'turn', created: 1767225600, status: 'failed', attempts: 2, generations: 2, trace_available: false,
    tools: [], requests: [], usage_recorded: false, ...overrides,
  }
}

function response(items: ActivityTurn[]) {
  return new Response(JSON.stringify({ items, total: items.length, offset: 0, limit: 25, coverage: { trace_days: 7, usage_days: 30 } }))
}

test('missing usage stays unknown while failures retain their distinct outcome', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(response([turn({
    usage_recorded: true,
    requests: [{ id: 'request', sequence: 1, provider: 'nvidia', operation: 'builder', model: 'nemotron-builder', started: 1767225600, finished: 1767225601,
      duration_ms: 1000, status: 'failed', input_tokens: null, output_tokens: null, cached_input_tokens: null, total_tokens: null, provider_status: null,
      error_type: 'timeout', http_status: 504 }],
  })]))))
  render(<ActivityPanel accountId="owner" conversationId="thread" refresh={0} />)
  await screen.findByText('1 requests · 0 tools')
  fireEvent.click(screen.getByText('1 requests · 0 tools').closest('summary')!)
  expect(screen.getAllByText('Not recorded')).toHaveLength(4)
  expect(screen.getByText('timeout · HTTP 504')).toBeTruthy()
  expect(screen.getByText('Detailed tool arguments and results are unavailable for this turn.')).toBeTruthy()
  expect(screen.queryByText('0 tokens')).toBeNull()
})

test('provider response received does not claim that the agent delivered a message', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(response([turn({
    status: 'running', trace_available: true, usage_recorded: true,
    requests: [{ id: 'request', sequence: 1, provider: 'openai', operation: 'answer', model: 'test-model', started: 1767225600, finished: 1767225601,
      duration_ms: 1000, status: 'received', input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, total_tokens: 0, provider_status: 'completed', error_type: null, http_status: null }],
  })]))))
  render(<ActivityPanel accountId="owner" conversationId="thread" refresh={0} />)
  await screen.findByText('1 requests · 0 tools')
  expect(screen.getByText('Response received')).toBeTruthy()
  expect(screen.getByText('Provider response received. The turn and message statuses show processing and delivery outcomes.')).toBeTruthy()
  expect(screen.getAllByText('0')).toHaveLength(4)
})
