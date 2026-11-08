import { Fragment, useState } from 'react'
import { ArrowLeft, ArrowRight, ArrowUpRight, ChevronDown, ChevronRight, FolderOpen, LayoutDashboard, Leaf, MessageSquare, RefreshCw, ShieldCheck, UserRound, Users } from 'lucide-react'
import { AccountSwitcher } from './components/AccountSwitcher'
import { ConversationPanel } from './components/ConversationPanel'
import { Badge, Button, Card, Empty, Pagination, RequestState, SearchInput, cn } from './components/ui'
import { accountPath, formatTime, useApi, type Account, type Overview, type Page, type Project, type ProjectDetail, type User } from './lib/api'

export default function App() {
  const [selection, setSelection] = useState<{ accountId: string; projectId: string | null; conversationId: string | null } | null>(null)
  const [refresh, setRefresh] = useState(0)
  function selectAccount(accountId: string, projectId: string | null = null) { setSelection({ accountId, projectId, conversationId: null }) }
  return <>
    <a href="#main" className="sr-only z-50 rounded-lg bg-primary p-3 text-primary-foreground focus:not-sr-only focus:fixed focus:left-4 focus:top-4">Skip to content</a>
    <header className="border-b bg-card">
      <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center justify-between gap-4 px-5 py-4 sm:px-8">
        <button onClick={() => setSelection(null)} className="flex items-center gap-3 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label="Acorn dashboard overview"><span className="flex size-11 items-center justify-center rounded-xl bg-primary text-primary-foreground"><Leaf className="size-6" aria-hidden="true" /></span><span><span className="block text-xl font-semibold tracking-tight">acorn</span><span className="block text-sm text-muted-foreground">Internal workspace</span></span></button>
        <div className="flex flex-wrap items-center gap-3"><Badge className="hidden sm:inline-flex"><ShieldCheck className="size-4" aria-hidden="true" />Local · Read only</Badge><AccountSwitcher accountId={selection?.accountId ?? null} onSelect={selectAccount} /></div>
      </div>
    </header>
    <main id="main" className="mx-auto max-w-screen-2xl space-y-7 px-5 py-8 sm:px-8 sm:py-10">
      {selection ? <>
        <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-primary/20 bg-accent px-5 py-4"><div className="flex items-start gap-3"><UserRound className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden="true" /><div><p className="font-medium">Viewing as <span className="break-all">{selection.accountId}</span></p><p className="mt-1 text-sm text-muted-foreground">Read-only view of this account’s workspace</p></div></div><Button variant="outline" onClick={() => setSelection(null)}><ArrowLeft />Back to overview</Button></div>
        <AccountView key={selection.accountId} accountId={selection.accountId} projectId={selection.projectId} conversationId={selection.conversationId} refresh={refresh} onRefresh={() => setRefresh(value => value + 1)} onProject={projectId => setSelection({ ...selection, projectId, conversationId: null })} onConversation={conversationId => setSelection({ ...selection, conversationId })} onBuilder={conversationId => setSelection({ ...selection, projectId: null, conversationId })} />
      </> : <OverviewView refresh={refresh} onRefresh={() => setRefresh(value => value + 1)} onSelect={selectAccount} />}
    </main>
    <footer className="mx-auto flex max-w-screen-2xl flex-wrap items-center justify-between gap-2 px-5 pb-6 text-sm text-muted-foreground sm:px-8"><span>Acorn internal dashboard</span><span>Personal development · Read-only access</span></footer>
  </>
}

function OverviewView({ refresh, onRefresh, onSelect }: { refresh: number; onRefresh: () => void; onSelect: (id: string, projectId?: string) => void }) {
  const request = useApi<Overview>('/api/internal/overview', refresh)
  return <>
    <div className="flex flex-wrap items-end justify-between gap-4"><div><p className="mb-2 flex items-center gap-2 text-sm font-medium text-primary"><LayoutDashboard className="size-4" aria-hidden="true" />Workspace overview</p><h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">A closer look at your agents.</h1><p className="mt-3 max-w-2xl leading-relaxed text-muted-foreground">Explore users, their projects, and the conversations happening inside them.</p></div><Button variant="outline" onClick={onRefresh}><RefreshCw />Refresh data</Button></div>
    <RequestState {...request} />
    {request.data && <><div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{[
      { label: 'Registered users', value: request.data.users, icon: Users, detail: 'Everyone who has messaged' },
      { label: 'Project owners', value: request.data.owners, icon: UserRound, detail: 'Users with an active project' },
      { label: 'Projects', value: request.data.projects, icon: FolderOpen, detail: 'Active agents across accounts' },
      { label: 'Conversations', value: request.data.conversations, icon: MessageSquare, detail: 'Threads with retained history' },
    ].map(stat => <Card key={stat.label} className="p-5"><div className="flex items-center justify-between gap-3"><p className="text-sm font-medium text-muted-foreground">{stat.label}</p><stat.icon className="size-5 text-primary" aria-hidden="true" /></div><p className="mt-5 text-4xl font-semibold tracking-tight tabular-nums">{stat.value.toLocaleString()}</p><p className="mt-2 text-sm text-muted-foreground">{stat.detail}</p></Card>)}</div><p className="text-sm text-muted-foreground">Snapshot updated {formatTime(request.data.updated_at)}</p></>}
    <UserTable refresh={refresh} onSelect={onSelect} />
  </>
}

function UserTable({ refresh, onSelect }: { refresh: number; onSelect: (id: string, projectId?: string) => void }) {
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)
  const request = useApi<Page<User>>(`/api/internal/users?q=${encodeURIComponent(query)}&offset=${offset}&limit=50`, refresh)
  return <Card id="users" className="overflow-hidden">
    <div className="flex flex-wrap items-center justify-between gap-5 border-b p-5 sm:p-6"><div><h2 className="text-xl font-semibold">Users <span className="ml-2 text-base font-normal text-muted-foreground">{request.data?.total.toLocaleString()}</span></h2><p className="mt-1 text-sm text-muted-foreground">Expand a user’s projects or open their account view.</p></div><SearchInput label="Search by phone number" value={query} onChange={event => { setQuery(event.target.value); setOffset(0); setExpanded(null) }} /></div>
    <RequestState {...request} />
    {request.data && <>
      {!request.data.items.length ? <Empty title={query ? 'No users found' : 'Your workspace is ready'}>{query ? 'Try searching for another phone number.' : 'Users and projects will appear after people start messaging your agents.'}</Empty> : <div className="overflow-x-auto"><table className="w-full min-w-3xl text-left text-sm"><thead className="bg-muted/40 text-muted-foreground"><tr><th className="px-5 py-4 font-medium">User</th><th className="px-4 py-4 font-medium">Projects</th><th className="px-4 py-4 font-medium">Conversations</th><th className="px-4 py-4 font-medium">Last active</th><th className="px-5 py-4"><span className="sr-only">Actions</span></th></tr></thead><tbody className="divide-y">
        {request.data.items.map(user => <Fragment key={user.id}><tr className="hover:bg-muted/30"><td className="px-5 py-3"><button className="flex min-h-11 items-center gap-3 rounded-lg pr-3 text-left font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-expanded={expanded === user.id} aria-controls={`projects-${user.id}`} onClick={() => setExpanded(expanded === user.id ? null : user.id)}>{expanded === user.id ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}<span className="flex size-9 items-center justify-center rounded-full bg-muted"><UserRound className="size-4 text-muted-foreground" aria-hidden="true" /></span>{user.phone}</button></td><td className="px-4 py-3 tabular-nums">{user.project_count}</td><td className="px-4 py-3 tabular-nums">{user.conversation_count}</td><td className="px-4 py-3 text-muted-foreground">{formatTime(user.last_active)}</td><td className="px-5 py-3 text-right"><Button variant="outline" onClick={() => onSelect(user.id)} aria-label={`View account ${user.phone}`}>View account<ArrowUpRight /></Button></td></tr>
          {expanded === user.id && <tr id={`projects-${user.id}`}><td colSpan={5} className="bg-muted/30 p-5"><ExpandedProjects accountId={user.id} refresh={refresh} onSelect={onSelect} /></td></tr>}
        </Fragment>)}
      </tbody></table></div>}
      <Pagination offset={offset} total={request.data.total} count={request.data.items.length} onChange={value => { setOffset(value); setExpanded(null) }} />
    </>}
  </Card>
}

function ExpandedProjects({ accountId, refresh, onSelect }: { accountId: string; refresh: number; onSelect: (id: string, projectId?: string) => void }) {
  const request = useApi<Account>(accountPath(accountId), refresh)
  return <><RequestState {...request} />{request.data && (request.data.projects.length ? <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{request.data.projects.map(project => <button key={project.id} className="flex items-center justify-between gap-3 rounded-lg border bg-card p-4 text-left hover:border-primary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onSelect(accountId, project.id)}><span><span className="block font-medium">{project.name}</span><span className="mt-1 block text-sm text-muted-foreground">{project.participant_count} participants · {project.conversation_count} conversations</span></span><ArrowUpRight className="size-4 shrink-0 text-primary" aria-hidden="true" /></button>)}</div> : <p className="text-sm text-muted-foreground">This user has no active projects. Open the account to view their builder conversation.</p>)}</>
}

function AccountView({ accountId, projectId, conversationId, refresh, onRefresh, onProject, onConversation, onBuilder }: {
  accountId: string; projectId: string | null; conversationId: string | null; refresh: number;
  onRefresh: () => void; onProject: (id: string | null) => void; onConversation: (id: string) => void; onBuilder: (id: string) => void
}) {
  const request = useApi<Account>(accountPath(accountId), refresh)
  const project = request.data?.projects.find(item => item.id === projectId)
  return <>
    <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground"><button onClick={() => onProject(null)} className="min-h-11 rounded-lg px-2 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Account workspace</button>{projectId && <><ChevronRight className="size-4" aria-hidden="true" /><span className="break-all font-medium text-foreground">{project?.name ?? 'Project'}</span></>}{!projectId && conversationId && <><ChevronRight className="size-4" aria-hidden="true" /><span className="font-medium text-foreground">Builder conversation</span></>}</nav>
    <RequestState {...request} />
    {request.data && <>
      {projectId ? <ProjectWorkspace key={projectId} accountId={accountId} projectId={projectId} conversationId={conversationId} refresh={refresh} onRefresh={onRefresh} onConversation={onConversation} /> : conversationId ? <ConversationPanel key={conversationId} accountId={accountId} conversationId={conversationId} refresh={refresh} /> : <>
        <div className="flex flex-wrap items-end justify-between gap-4"><div><h1 className="text-3xl font-semibold tracking-tight">Account workspace</h1><p className="mt-2 text-muted-foreground">{request.data.projects.length} projects · Last active {formatTime(request.data.user.last_active)}</p></div><Button variant="outline" onClick={onRefresh}><RefreshCw />Refresh data</Button></div>
        <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">{request.data.projects.map(item => <ProjectCard key={item.id} project={item} onSelect={() => onProject(item.id)} />)}</div>
        {!request.data.projects.length && <Card><Empty title="No active projects">This account has not created a project yet. Its builder conversation is available below when it has retained messages.</Empty></Card>}
        <Card className="flex flex-wrap items-center justify-between gap-5 p-6"><div className="flex items-start gap-4"><span className="rounded-xl bg-accent p-3 text-primary"><MessageSquare className="size-5" aria-hidden="true" /></span><div><h2 className="text-lg font-semibold">Builder conversation</h2><p className="mt-1 max-w-2xl text-sm leading-relaxed text-muted-foreground">This account’s conversation with Acorn, including project creation and setup.</p>{request.data.builder_conversation ? <p className="mt-2 text-sm text-muted-foreground">{request.data.builder_conversation.message_count} retained messages</p> : <p className="mt-2 text-sm text-muted-foreground">No retained builder messages yet.</p>}</div></div>{request.data.builder_conversation && <Button variant="outline" onClick={() => onBuilder(request.data!.builder_conversation!.id)}>Open conversation<ArrowRight /></Button>}</Card>
      </>}
    </>}
  </>
}

function ProjectCard({ project, onSelect }: { project: Project; onSelect: () => void }) {
  return <Card className="flex flex-col p-6"><div className="flex items-center justify-between gap-3"><span className="rounded-xl bg-accent p-3 text-primary"><FolderOpen className="size-5" aria-hidden="true" /></span><Badge>{project.code}</Badge></div><h2 className="mt-5 break-words text-xl font-semibold">{project.name}</h2><p className="mt-2 text-sm text-muted-foreground">{project.participant_count} participants · {project.conversation_count} conversations</p><p className="mb-6 mt-2 text-sm text-muted-foreground">{formatTime(project.last_active)}</p><Button className="mt-auto w-full" variant="outline" onClick={onSelect}>Explore project<ArrowRight /></Button></Card>
}

function ProjectWorkspace({ accountId, projectId, conversationId, refresh, onRefresh, onConversation }: { accountId: string; projectId: string; conversationId: string | null; refresh: number; onRefresh: () => void; onConversation: (id: string) => void }) {
  const request = useApi<ProjectDetail>(`${accountPath(accountId)}/projects/${encodeURIComponent(projectId)}`, refresh)
  const [query, setQuery] = useState('')
  return <><RequestState {...request} />{request.data && <>
    <div className="flex flex-wrap items-end justify-between gap-4"><div><div className="mb-3"><Badge>{request.data.project.code}</Badge></div><h1 className="break-words text-3xl font-semibold tracking-tight">{request.data.project.name}</h1><p className="mt-2 text-muted-foreground">{request.data.project.participant_count} participants · {request.data.project.conversation_count} conversations</p></div><Button variant="outline" onClick={onRefresh}><RefreshCw />Refresh data</Button></div>
    <div className="grid items-start gap-5 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <Card className="overflow-hidden"><div className="space-y-4 border-b p-5"><h2 className="font-semibold">Participants</h2><SearchInput label="Find a participant" value={query} onChange={event => setQuery(event.target.value)} /></div><div className="max-h-96 overflow-y-auto p-2 lg:max-h-[36rem]">{request.data.participants.filter(item => item.phone.includes(query)).map(participant => <button key={participant.id} onClick={() => onConversation(participant.conversation_id)} aria-pressed={conversationId === participant.conversation_id} className={cn('w-full rounded-lg p-3 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring', conversationId === participant.conversation_id && 'bg-accent text-primary')}><span className="block break-all font-medium">{participant.phone}</span><span className="mt-1 block text-sm text-muted-foreground">{participant.message_count} messages</span><span className="mt-1 block text-sm text-muted-foreground">{formatTime(participant.last_active)}</span></button>)}{!request.data.participants.filter(item => item.phone.includes(query)).length && <Empty title={query ? 'No matching participants' : 'No conversations yet'}>{query ? 'Try another phone number.' : 'Participants appear after messaging this agent.'}</Empty>}</div></Card>
      {conversationId ? <ConversationPanel key={conversationId} accountId={accountId} conversationId={conversationId} refresh={refresh} /> : <Card><Empty title="Select a participant"><MessageSquare className="mx-auto my-5 size-10 text-primary/60" aria-hidden="true" />Choose a participant to read their conversation with this agent.</Empty></Card>}
    </div>
  </>}</>
}
