/** Kept independent of the host so authorization/CAS failures can be tested offline. */
export function authorize(sender: string, backend: string): void {
  if (!/^[0-9a-f]{64}$/i.test(backend) || sender.toLowerCase() !== backend.toLowerCase()) {
    throw new Error('backend identity required');
  }
}

export function validateCommit(expected: bigint, actual: bigint, value: string): void {
  if (expected !== actual) throw new Error('state revision conflict');
  const parsed: unknown = JSON.parse(value);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('state must be a JSON object');
  }
}
