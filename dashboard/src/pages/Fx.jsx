import { useEffect, useMemo, useState } from 'react'
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { fetchFx, fetchOpportunities } from '../lib/data'
import { Empty, ErrorBox, Loading, Stat } from '../components/Common'
import { carName, chf, pct, signClass } from '../lib/format'

const AXIS = { stroke: '#6b7280', fontSize: 11 }
const TOOLTIP = {
  contentStyle: { background: '#12151a', border: '1px solid #2a313b', borderRadius: 8, fontSize: 12 },
  labelStyle: { color: '#9ca3af' },
}

/**
 * What a fractional FX move is worth on one car's landed cost.
 * The tax chain amplifies it: freight and the FOB price sit inside the
 * customs value, so 4% Automobilsteuer and 8.1% VAT ride on top of the move.
 */
function marginImpact(priceUsd, move, usdChf) {
  return -(priceUsd * usdChf * move) * 1.04 * 1.081
}

function moveOver(rows, field, days) {
  if (!rows || rows.length < 2) return null
  const latest = rows[rows.length - 1]
  const cutoff = new Date(new Date(latest.day).getTime() - days * 864e5)
    .toISOString().slice(0, 10)
  const earlier = rows.filter((r) => r.day <= cutoff)
  const base = earlier.length ? earlier[earlier.length - 1] : rows[0]
  if (!base[field]) return null
  return (latest[field] - base[field]) / base[field]
}

export default function Fx() {
  const [rows, setRows] = useState(null)
  const [opps, setOpps] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    Promise.all([fetchFx({ days: 180 }), fetchOpportunities({ max: 50 })])
      .then(([f, o]) => { setRows(f); setOpps(o) })
      .catch(setError)
  }, [])

  const series = useMemo(
    () => (rows || []).map((r) => ({
      day: String(r.day).slice(5),
      usd: r.usd_chf,
      // JPY/CHF is ~0.005, which is invisible on the same axis as USD/CHF.
      // Show it per 100 yen, which is how it is quoted in practice anyway.
      jpy100: r.jpy_chf ? r.jpy_chf * 100 : null,
    })),
    [rows],
  )

  const usdMove7 = moveOver(rows, 'usd_chf', 7)
  const jpyMove7 = moveOver(rows, 'jpy_chf', 7)
  const latest = rows?.[rows.length - 1]

  const annotations = useMemo(() => {
    if (!latest || usdMove7 == null) return []
    const seen = new Set()
    return opps
      .filter((o) => o.price_usd && o.watchlist_key && !seen.has(o.watchlist_key)
                     && seen.add(o.watchlist_key))
      .slice(0, 5)
      .map((o) => ({
        name: carName(o),
        impact: marginImpact(o.price_usd, usdMove7, latest.usd_chf),
        hypothetical: marginImpact(o.price_usd, -0.03, latest.usd_chf),
      }))
  }, [opps, latest, usdMove7])

  if (error) return <ErrorBox error={error} />
  if (!rows) return <Loading what="FX history" />
  if (!rows.length) {
    return <Empty>No FX history yet — run <code>nippon-margin backfill</code>.</Empty>
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="USD/CHF" value={latest?.usd_chf?.toFixed(4) ?? '—'} />
        <Stat label="USD 7d" value={pct(usdMove7)} tone={signClass(usdMove7 == null ? null : -usdMove7)} />
        <Stat label="JPY/CHF ×100" value={latest ? (latest.jpy_chf * 100).toFixed(4) : '—'} />
        <Stat label="JPY 7d" value={pct(jpyMove7)} tone={signClass(jpyMove7 == null ? null : -jpyMove7)} />
      </div>

      <section className="card p-3">
        <h2 className="mb-1 text-sm font-semibold">USD/CHF</h2>
        <p className="mb-2 text-xs text-neutral-500">
          A falling line means the franc buys more dollars — Japanese stock gets cheaper for us.
        </p>
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={series} margin={{ left: -8, right: 8, top: 8 }}>
            <CartesianGrid stroke="#252b34" strokeDasharray="2 4" />
            <XAxis dataKey="day" {...AXIS} minTickGap={28} />
            <YAxis {...AXIS} domain={['auto', 'auto']} tickFormatter={(v) => v.toFixed(3)} />
            <Tooltip {...TOOLTIP} formatter={(v) => Number(v).toFixed(4)} />
            <Line type="monotone" dataKey="usd" name="USD/CHF" stroke="#60a5fa" dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      </section>

      <section className="card p-3">
        <h2 className="mb-1 text-sm font-semibold">JPY/CHF (per 100 yen)</h2>
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={series} margin={{ left: -8, right: 8, top: 8 }}>
            <CartesianGrid stroke="#252b34" strokeDasharray="2 4" />
            <XAxis dataKey="day" {...AXIS} minTickGap={28} />
            <YAxis {...AXIS} domain={['auto', 'auto']} tickFormatter={(v) => v.toFixed(3)} />
            <Tooltip {...TOOLTIP} formatter={(v) => Number(v).toFixed(4)} />
            <Line type="monotone" dataKey="jpy100" name="JPY/CHF ×100" stroke="#fbbf24" dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      </section>

      <section className="card p-3">
        <h2 className="mb-1 text-sm font-semibold">Margin impact</h2>
        <p className="mb-3 text-xs text-neutral-500">
          FX is a core margin driver: the customs value moves with it, so 4%
          Automobilsteuer and 8.1% VAT amplify every move by ~12%.
        </p>
        <ul className="space-y-2 text-sm">
          {annotations.map((a) => (
            <li key={a.name} className="border-b border-edge/40 pb-2">
              <div className="font-medium">{a.name}</div>
              <div className="text-neutral-400">
                Last 7 days ({pct(usdMove7)}):{' '}
                <span className={signClass(a.impact)}>
                  {a.impact >= 0 ? '−' : '+'}{chf(Math.abs(a.impact))} landed cost
                </span>
              </div>
              <div className="text-neutral-500">
                A further 3% weakening would add {chf(Math.abs(a.hypothetical))} to the spread.
              </div>
            </li>
          ))}
          {annotations.length === 0 && (
            <li className="text-neutral-500">No scored opportunities to annotate yet.</li>
          )}
        </ul>
      </section>
    </div>
  )
}
