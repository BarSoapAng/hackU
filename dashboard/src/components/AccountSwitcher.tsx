import { useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { ArrowUpRight, ChevronsUpDown, UserRound, X } from 'lucide-react'
import { useApi, type Page, type User } from '../lib/api'
import { Badge, Button, Empty, Pagination, RequestState, SearchInput } from './ui'

export function AccountSwitcher({ accountId, onSelect }: { accountId: string | null; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><Button variant="outline"><UserRound />{accountId ? 'Switch account' : 'View as user'}<ChevronsUpDown /></Button></Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-40 bg-stone-950/40" />
      <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[90dvh] w-[calc(100%-2rem)] max-w-xl -translate-x-1/2 -translate-y-1/2 flex-col rounded-2xl border bg-card shadow-xl focus:outline-none">
        <div className="relative border-b p-6 pr-16"><Dialog.Title className="text-xl font-semibold">View as user</Dialog.Title><Dialog.Description className="mt-2 text-sm leading-relaxed text-muted-foreground">Choose an account to explore its Sprouts and conversations. All views are read only.</Dialog.Description><Dialog.Close asChild><Button variant="ghost" className="absolute right-3 top-3 px-3" aria-label="Close account switcher"><X /></Button></Dialog.Close></div>
        {open && <AccountPicker accountId={accountId} onSelect={id => { onSelect(id); setOpen(false) }} />}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}

function AccountPicker({ accountId, onSelect }: { accountId: string | null; onSelect: (id: string) => void }) {
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const request = useApi<Page<User>>(`/api/internal/users?q=${encodeURIComponent(query)}&offset=${offset}&limit=50`)
  return <>
    <div className="p-5"><SearchInput label="Search by phone number" value={query} onChange={event => { setQuery(event.target.value); setOffset(0) }} /></div>
    <div className="min-h-0 overflow-y-auto px-3 pb-3">
      <RequestState {...request} />
      {request.data?.items.map(user => <button key={user.id} onClick={() => onSelect(user.id)} className="flex min-h-20 w-full items-center justify-between gap-4 rounded-lg px-3 py-3 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span className="min-w-0"><span className="block break-all font-medium">{user.phone}</span><span className="mt-1 block text-sm text-muted-foreground">{user.project_count} Sprouts · {user.conversation_count} conversations</span></span>
        {user.id === accountId ? <Badge className="bg-accent text-primary">Viewing</Badge> : <ArrowUpRight aria-hidden="true" className="size-5 shrink-0 text-muted-foreground" />}
      </button>)}
      {request.data && !request.data.items.length && <Empty title={query ? 'No matching users' : 'No users yet'}>{query ? 'Try another phone number.' : 'Users appear after their first accepted message.'}</Empty>}
    </div>
    {request.data && <Pagination offset={offset} count={request.data.items.length} total={request.data.total} onChange={setOffset} />}
  </>
}
