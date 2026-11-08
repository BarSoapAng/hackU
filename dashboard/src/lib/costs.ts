export function formatUsd(value: number) {
  if (value > 0 && value < 0.01) return '<$0.01'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)
}

export function providerName(provider: string) {
  return provider === 'nvidia' ? 'NVIDIA / Brev' : provider === 'spacetimedb' ? 'SpacetimeDB' : provider === 'openai' ? 'OpenAI' : provider
}
