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
