import { schema, table, t, SenderError } from 'spacetimedb/server';
import { authorize, validateCommit } from './policy';

const appState = table(
  { name: 'app_state', public: false },
  { id: t.u8().primaryKey(), revision: t.u64(), value: t.string() },
);

const spacetimedb = schema(
  { appState },
  { env: { BACKEND_IDENTITY: t.string() } },
);
export default spacetimedb;

function requireBackend(sender: string, backend: string): void {
  try {
    authorize(sender, backend);
  } catch {
    throw new SenderError('backend identity required');
  }
}

export const init = spacetimedb.init(ctx => {
  // A publish-time identity is mandatory; there is no first-caller bootstrap.
  authorize(ctx.env.BACKEND_IDENTITY, ctx.env.BACKEND_IDENTITY);
  ctx.db.appState.insert({ id: 0, revision: 0n, value: '{}' });
});

export const onConnect = spacetimedb.clientConnected(ctx => {
  requireBackend(ctx.sender.toHexString(), ctx.env.BACKEND_IDENTITY);
});

export const read_state = spacetimedb.procedure(t.string(), ctx =>
  ctx.withTx(tx => {
    requireBackend(tx.sender.toHexString(), tx.env.BACKEND_IDENTITY);
    const row = tx.db.appState.id.find(0);
    // Updating the existing empty module does not rerun its init reducer.
    return JSON.stringify({ revision: (row?.revision ?? 0n).toString(), state: JSON.parse(row?.value ?? '{}') });
  }),
);

export const commit_state = spacetimedb.reducer(
  { expected: t.u64(), value: t.string() },
  (ctx, { expected, value }) => {
    requireBackend(ctx.sender.toHexString(), ctx.env.BACKEND_IDENTITY);
    const row = ctx.db.appState.id.find(0);
    try {
      validateCommit(expected, row?.revision ?? 0n, value);
    } catch (error) {
      throw new SenderError(error instanceof Error ? error.message : 'invalid state');
    }
    if (row) ctx.db.appState.id.update({ id: 0, revision: row.revision + 1n, value });
    else ctx.db.appState.insert({ id: 0, revision: 1n, value });
  },
);
