// Integers stay exact. Number conversion is confined to chart coordinates/count ratios.
export function formatMeasurement(
  value: string | number | null | undefined,
  unit: 'cpu_microseconds' | 'bytes',
) {
  if (value == null) return '—'
  const raw = BigInt(value)
  const scale = unit === 'bytes' ? 1048576n : 1000n
  const milli = (raw * 1000n + scale / 2n) / scale
  const fraction = String(milli % 1000n)
    .padStart(3, '0')
    .replace(/0+$/, '')
  return fraction ? `${milli / 1000n}.${fraction}` : String(milli / 1000n)
}

export function chartPosition(value: string, end: string) {
  const ceiling = BigInt(end)
  if (ceiling <= 0n) return 0
  return Number((BigInt(value) * 1000000n) / ceiling) / 1000000
}
