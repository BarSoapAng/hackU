import assert from 'node:assert/strict';
import test from 'node:test';
import { authorize, validateCommit } from '../src/policy.ts';

test('only the configured backend identity is accepted', () => {
  authorize('a'.repeat(64), 'A'.repeat(64));
  assert.throws(() => authorize('b'.repeat(64), 'a'.repeat(64)), /identity/);
  assert.throws(() => authorize('', 'a'.repeat(64)), /identity/);
  assert.throws(() => authorize('', ''), /identity/);
});

test('stale revisions and malformed snapshots cannot commit', () => {
  validateCommit(7n, 7n, '{"users":{}}');
  assert.throws(() => validateCommit(6n, 7n, '{}'), /revision conflict/);
  for (const value of ['null', '[]', '1', '"text"', 'not json']) {
    assert.throws(() => validateCommit(7n, 7n, value));
  }
});

function initial(): any {
  return {
    projects: { p: { id: 'p', owner: 'owner', code: 'PUBLIC', deleted: false } },
    files: {}, chunks: {}, capabilities: {},
    jobs: { turn: { id: 'turn', kind: 'turn', user: 'owner', created: 100, attempts: 1,
      generations: 1, status: 'running', steps: [] } },
    receipts: { event: { id: 'event', created: 100, status: 'accepted' } },
  };
}

function verify(next: any, before: any = {}, now = 100): void {
  validateCommit(1n, 1n, JSON.stringify(next), JSON.stringify(before), now);
}

test('project ownership, stable codes and deletion tombstones cannot be rewritten', () => {
  const before = initial();
  verify(before);
  for (const change of [
    (next: any) => { next.projects.p.owner = 'intruder'; },
    (next: any) => { next.projects.p.code = 'REPLACED'; },
    (next: any) => { delete next.projects.p; },
    (next: any) => { next.projects.other = { ...next.projects.p, id: 'other' }; },
  ]) {
    const next = structuredClone(before);
    change(next);
    assert.throws(() => verify(next, before));
  }
  before.projects.p.deleted = true;
  const revived = structuredClone(before);
  revived.projects.p.deleted = false;
  assert.throws(() => verify(revived, before), /deletion/);
});

test('project and vault ceilings are checked on the complete proposed snapshot', () => {
  const state = initial();
  for (let i = 0; i < 5; i++) state.projects['p' + i] = { id: 'p' + i, owner: 'owner', code: 'CODE' + i };
  assert.throws(() => verify(state), /project quota/);
  state.projects = initial().projects;
  for (let i = 0; i < 21; i++) {
    state.files['f' + i] = { id: 'f' + i, project: 'p', version: 1, name: i + '.md', content: 'a', bytes: 1, chunk_count: 1 };
  }
  assert.throws(() => verify(state), /vault quota/);
  state.files = { f: { id: 'f', project: 'p', version: 1, name: 'f.md', content: 'a', bytes: 1, chunk_count: 1001 } };
  assert.throws(() => verify(state), /quota/);
  state.files.f.chunk_count = 1;
  state.files.f.bytes = 9;
  assert.throws(() => verify(state), /byte count/);
});

test('file content changes require consecutive versions and cannot revive deleted IDs', () => {
  const before = initial();
  before.files.f = { id: 'f', project: 'p', version: 1, name: 'f.md', content: 'a', bytes: 1, chunk_count: 1, status: 'pending' };
  before.jobs.index = { id: 'index', kind: 'index', file: 'f', version: 1, created: 100, attempts: 0, status: 'queued' };
  const next = structuredClone(before);
  next.files.f.content = 'b';
  assert.throws(() => verify(next, before), /version conflict/);
  next.files.f.version = 2;
  verify(next, before);
  next.files.f.version = 0;
  assert.throws(() => verify(next, before), /version conflict/);
  delete before.files.f;
  next.files.f.version = 1;
  assert.throws(() => verify(next, before), /recreated/);
});

test('only current ready file chunks can be committed', () => {
  const state = initial();
  state.files.f = { id: 'f', project: 'p', version: 1, name: 'f.md', content: 'a', bytes: 1, chunk_count: 1, status: 'ready' };
  state.chunks.f = [{ file_id: 'f', version: 1 }];
  verify(state);
  state.chunks.f[0].version = 0;
  assert.throws(() => verify(state), /current indexed/);
  state.chunks.f[0].version = 1;
  state.files.f.status = 'pending';
  assert.throws(() => verify(state), /current indexed/);
});

test('completed tool results and continuation bindings survive retries unchanged', () => {
  const before = initial();
  before.jobs.turn.steps = [{ id: 'turn:0', name: 'create_project', target: null, expected_revision: 0,
    result: { id: 'p', code: 'PUBLIC' }, resulting_revision: 1, continuation: { focus: 'p', revision: 1 } }];
  for (const change of [
    (next: any) => { next.jobs.turn.steps = []; },
    (next: any) => { next.jobs.turn.steps[0].result.id = 'another'; },
    (next: any) => { next.jobs.turn.steps[0].continuation.revision = 3; },
    (next: any) => { next.jobs.turn.steps[0].name = 'issue_vault_link'; },
  ]) {
    const next = structuredClone(before);
    change(next);
    assert.throws(() => verify(next, before), /tool/);
  }
  const replay = structuredClone(before);
  replay.jobs.turn.steps[0].result = { code: 'PUBLIC', id: 'p' };
  verify(replay, before); // JSON key order is irrelevant.
});

test('generation, tool, attempt and pending-turn budgets cannot be bypassed', () => {
  for (const change of [
    (state: any) => { state.jobs.turn.generations = 5; },
    (state: any) => { state.jobs.turn.tool_calls = 4; },
    (state: any) => { state.jobs.turn.attempts = 6; },
    (state: any) => { state.jobs.turn.steps = [{}, {}, {}, {}]; },
    (state: any) => { state.jobs.turn.attempts = -1; },
    (state: any) => {
      for (let i = 0; i < 10; i++) state.jobs['j' + i] = { ...state.jobs.turn, id: 'j' + i };
    },
  ]) {
    const state = initial();
    change(state);
    assert.throws(() => verify(state), /quota|counter/);
  }
  const before = initial(), next = initial();
  next.jobs.turn.generations = 0;
  assert.throws(() => verify(next, before), /cannot be reset/);
  next.jobs.turn.generations = before.jobs.turn.generations;
  before.jobs.turn.tool_calls = 1;
  next.jobs.turn.tool_calls = 0;
  assert.throws(() => verify(next, before), /cannot be reset/);
});

test('recent deduplication cannot be discarded and active work cannot age out', () => {
  const before = initial(), next = initial();
  delete next.receipts.event;
  assert.throws(() => verify(next, before), /deduplication/);
  verify(next, before, 100 + 31 * 86400);
  delete next.jobs.turn;
  assert.throws(() => verify(next, before, 100 + 31 * 86400), /deduplication/);
});

test('capability expiry/scope cannot be extended and revocation is irreversible', () => {
  const before = initial();
  before.capabilities.cap = { hash: 'hash', expires: 3700, owner: 'owner', project: 'p', job: 'turn', step: 'turn:0', revoked: true };
  const next = structuredClone(before);
  next.capabilities.cap.revoked = false;
  assert.throws(() => verify(next, before), /immutable/);
  next.capabilities.cap.revoked = true;
  next.capabilities.cap.expires++;
  assert.throws(() => verify(next, before), /immutable/);
  delete next.capabilities.cap;
  verify(next, before); // Expiry cleanup keeps the durable tool result for deduplication.
});

test('retained tool results prevent recreating a purged capability', () => {
  const before = initial();
  before.jobs.turn.steps = [{ id: 'turn:0', name: 'issue_vault_link', target: 'p', expected_revision: 1,
    result: { capability: 'expired-cap', expires: 50 } }];
  const next = structuredClone(before);
  next.capabilities['expired-cap'] = { hash: 'new-token', expires: 3700, revoked: false };
  assert.throws(() => verify(next, before), /cannot be recreated/);
});
