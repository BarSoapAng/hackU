/** Kept independent of the host so authorization/CAS failures can be tested offline. */
export function authorize(sender: string, backend: string): void {
  if (!/^[0-9a-f]{64}$/i.test(backend) || sender.toLowerCase() !== backend.toLowerCase()) {
    throw new Error('backend identity required');
  }
}

function object(value: unknown): Record<string, any> {
  if (value === null || Array.isArray(value) || typeof value !== 'object') {
    throw new Error('state must contain JSON objects');
  }
  return value as Record<string, any>;
}

function integer(value: unknown, maximum: number): asserts value is number {
  if (!Number.isSafeInteger(value) || (value as number) < 0 || (value as number) > maximum) {
    throw new Error('state quota or counter exceeded');
  }
}

function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}

export function validateCommit(expected: bigint, actual: bigint, value: string, previous = '{}', now = 0): void {
  if (expected !== actual) throw new Error('state revision conflict');
  const next = object(JSON.parse(value));
  const before = object(JSON.parse(previous));
  for (const name of ['users', 'assignments', 'receipts', 'jobs', 'projects', 'files', 'chunks',
    'capabilities', 'delivery_material', 'outbox', 'conversations', 'usage']) {
    object(next[name] ?? {});
    object(before[name] ?? {});
  }
  const projects = object(next.projects ?? {});
  const oldProjects = object(before.projects ?? {});
  const files = object(next.files ?? {});
  const oldFiles = object(before.files ?? {});
  const jobs = object(next.jobs ?? {});
  const oldJobs = object(before.jobs ?? {});
  const ownerCounts: Record<string, number> = {};
  const codes = new Set<string>();
  let activeProjects = 0;
  for (const [id, project] of Object.entries(projects)) {
    object(project);
    if (project.id !== id || typeof project.owner !== 'string' || !project.owner ||
        typeof project.code !== 'string' || !project.code || codes.has(project.code)) {
      throw new Error('invalid or reused project identity');
    }
    codes.add(project.code);
    const old = oldProjects[id];
    if (old && (old.owner !== project.owner || old.code !== project.code || (old.deleted && !project.deleted))) {
      throw new Error('project ownership, public code and deletion are immutable');
    }
    if (!project.deleted) {
      activeProjects++;
      ownerCounts[project.owner] = (ownerCounts[project.owner] ?? 0) + 1;
      if (activeProjects > 100 || ownerCounts[project.owner] > 5) throw new Error('project quota exceeded');
    }
  }
  // Keep compact project identities/tombstones: a delayed snapshot cannot recycle a code.
  for (const id of Object.keys(oldProjects)) {
    if (!projects[id]) throw new Error('project tombstone must be retained');
  }
  const notesBySprout: Record<string, { count: number; bytes: number; chunks: number; names: Set<string> }> = {};
  for (const [id, file] of Object.entries(files)) {
    object(file);
    if (file.id !== id || !projects[file.project] || projects[file.project].deleted ||
        typeof file.name !== 'string' || typeof file.content !== 'string') {
      throw new Error('invalid file scope');
    }
    integer(file.version, Number.MAX_SAFE_INTEGER);
    integer(file.bytes, 131072);
    integer(file.chunk_count, 1000);
    if (new TextEncoder().encode(file.content).length !== file.bytes) throw new Error('incorrect file byte count');
    const old = oldFiles[id];
    if (old) {
      if (file.project !== old.project || file.version < old.version || file.version > old.version + 1 ||
          ((old.content !== file.content || old.name !== file.name) && file.version !== old.version + 1)) {
        throw new Error('file version conflict');
      }
    } else if (file.version !== 1 || Object.values(oldJobs).some(job => job.kind === 'index' && job.file === id)) {
      throw new Error('file identity cannot be recreated');
    }
    const notes = notesBySprout[file.project] ??= { count: 0, bytes: 0, chunks: 0, names: new Set() };
    if (notes.names.has(file.name.toLowerCase())) throw new Error('duplicate note name');
    notes.names.add(file.name.toLowerCase());
    notes.count++;
    notes.bytes += file.bytes;
    notes.chunks += file.chunk_count;
    if (notes.count > 20 || notes.bytes > 1048576 || notes.chunks > 1000) throw new Error('notes quota exceeded');
  }
  for (const [id, chunks] of Object.entries(object(next.chunks ?? {}))) {
    const file = files[id];
    if (!file || file.status !== 'ready' || !Array.isArray(chunks) || chunks.length !== file.chunk_count ||
        chunks.some(chunk => chunk.file_id !== id || chunk.version !== file.version)) {
      throw new Error('chunks must match the current indexed file');
    }
  }
  let pending = 0;
  const pendingUsers: Record<string, number> = {};
  for (const [id, job] of Object.entries(jobs)) {
    object(job);
    if (job.id !== id) throw new Error('invalid job identity');
    integer(job.attempts, 5);
    integer(job.generations ?? 0, 4);
    integer(job.tool_calls ?? 0, 3);
    if (!Array.isArray(job.steps ?? []) || (job.steps ?? []).length > 3) throw new Error('tool quota exceeded');
    if (job.kind === 'turn' && ['queued', 'running'].includes(job.status)) {
      pending++;
      pendingUsers[job.user] = (pendingUsers[job.user] ?? 0) + 1;
      if (pending > 100 || pendingUsers[job.user] > 10) throw new Error('pending turn quota exceeded');
    }
    const old = oldJobs[id];
    if (!old) continue;
    if (job.created !== old.created || job.kind !== old.kind || job.user !== old.user ||
        job.attempts < old.attempts || (job.generations ?? 0) < (old.generations ?? 0) ||
        (job.tool_calls ?? 0) < (old.tool_calls ?? 0)) {
      throw new Error('job identity and attempt counters cannot be reset');
    }
    for (const step of old.steps ?? []) {
      const current = (job.steps ?? []).find((candidate: any) => candidate.id === step.id);
      if (!current) throw new Error('tool step must be retained');
      for (const key of ['name', 'target', 'expected_revision']) {
        if (canonical(current[key]) !== canonical(step[key])) throw new Error('tool intent is immutable');
      }
      if ('result' in step) {
        for (const key of ['result', 'resulting_revision', 'continuation']) {
          if (canonical(current[key]) !== canonical(step[key])) throw new Error('completed tool result is immutable');
        }
      }
    }
  }
  for (const name of ['jobs', 'receipts', 'outbox']) {
    for (const [id, row] of Object.entries(object(before[name] ?? {}))) {
      if (!(next[name] ?? {})[id] && (row.created >= now - 30 * 86400 || ['queued', 'running', 'submitting'].includes(row.status))) {
        throw new Error('deduplication record must be retained');
      }
    }
  }
  for (const [id, cap] of Object.entries(object(next.capabilities ?? {}))) {
    const old = (before.capabilities ?? {})[id];
    if (!old) {
      if (Object.values(oldJobs).some(job => (job.steps ?? []).some((step: any) => step.result?.capability === id))) {
        throw new Error('expired or revoked capability cannot be recreated');
      }
      continue;
    }
    if (['hash', 'expires', 'owner', 'project', 'job', 'step'].some(key => cap[key] !== old[key]) ||
        (old.revoked && !cap.revoked) || (old.delivered && !cap.delivered)) {
      throw new Error('capability scope, expiry and revocation are immutable');
    }
  }
}
