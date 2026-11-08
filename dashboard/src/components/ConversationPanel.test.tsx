import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { ConversationPanel } from './ConversationPanel'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test('an earlier transcript page remains navigable when retention shrinks on refresh', async () => {
  let retained = 100
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    const offset = Number(new URL(path, 'http://localhost').searchParams.get('offset'))
    return Promise.resolve(new Response(JSON.stringify({
      id: 'thread', kind: 'project', participant_id: '+15550000001', project_id: 'project',
      total: retained, offset, limit: 50, coverage: { message_days: 30, message_limit: 200, trace_days: 7 },
      messages: offset < retained ? [{ role: 'user', content: `Page ${offset}`, created: 1767225600, job_id: null, delivery_status: null }] : [],
    })))
  }))
  const user = userEvent.setup()
  render(<ConversationPanel accountId="owner" conversationId="thread" refresh={0} />)
  await screen.findByText('Page 0')
  expect(screen.getByText('With +15550000001')).toBeTruthy()
  await user.click(screen.getByRole('button', { name: 'Earlier messages' }))
  await screen.findByText('Page 50')
  retained = 30
  await user.click(screen.getByRole('button', { name: 'Refresh conversation' }))
  await screen.findByText('No messages on this page')
  await user.click(screen.getByRole('button', { name: 'Newer messages' }))
  await screen.findByText('Page 0')
  await waitFor(() => expect(screen.queryByText('No messages on this page')).toBeNull())
})
