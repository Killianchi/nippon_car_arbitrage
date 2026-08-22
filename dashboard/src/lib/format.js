export const chf = (v, opts = {}) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : new Intl.NumberFormat('de-CH', {
        style: 'currency', currency: 'CHF',
        maximumFractionDigits: 0, ...opts,
      }).format(v)

export const usd = (v) =>
  v === null || v === undefined
    ? '—'
    : new Intl.NumberFormat('en-US', {
        style: 'currency', currency: 'USD', maximumFractionDigits: 0,
      }).format(v)

export const pct = (v, digits = 1) =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`

export const km = (v) =>
  v === null || v === undefined ? '—' : `${new Intl.NumberFormat('de-CH').format(v)} km`

export const num = (v, digits = 2) =>
  v === null || v === undefined ? '—' : Number(v).toFixed(digits)

export const shortDate = (iso) => (iso ? String(iso).slice(0, 10) : '—')

export const carName = (o) =>
  [o.year, o.make, o.variant || o.model].filter(Boolean).join(' ')

/** Green above zero, red below. Used for margin and spread columns. */
export const signClass = (v) =>
  v === null || v === undefined ? 'text-neutral-400' : v >= 0 ? 'text-pos' : 'text-neg'
