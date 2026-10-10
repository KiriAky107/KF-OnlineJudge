// Integers stay exact. Number conversion is confined to chart coordinates/count ratios.
export function formatMeasurement(value: string | null, unit: 'cpu_microseconds' | 'bytes') {
  if (value === null) return '—'
  const raw = BigInt(value)
  const scale = unit === 'bytes' ? 1048576n : 1000n
  const places = unit === 'bytes' ? 6 : 3
  const fraction = ((raw % scale) * 10n ** BigInt(places)) / scale
  if (raw > 0n && raw < scale && fraction === 0n) return '<0.' + '0'.repeat(places - 1) + '1'
  return `${raw / scale}.${String(fraction).padStart(places, '0')}`.replace(/\.?0+$/, '') || '0'
}

export function chartPosition(value: string, end: string) {
  const ceiling = BigInt(end)
  if (ceiling <= 0n) return 0
  return Number((BigInt(value) * 1000000n) / ceiling) / 1000000
}
