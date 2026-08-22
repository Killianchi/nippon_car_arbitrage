import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts'
import { fetchModelStats, fetchOpportunities } from '../lib/data'
import { Empty, ErrorBox, Loading, Stat } from '../components/Common'
import { chf, num, pct, signClass } from '../lib/format'

const AXIS = { stroke: '#6b7280', fontSize: 11 }
const GRID = { stroke: '#252b34' }
const TOOLTIP = {
  contentStyle: { background: '#12151a', border: '1px solid #2a313b', borderRadius: 8, fontSize: 12 },
  labelStyle: { color: '#9ca3af' },
}

export default function ModelDetail() {
  const { key } = useParams()
  const navigate = useNavigate()
  const [stats, setStats] = useState(null)
  const [opps, setOpps] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    Promise.all([fetchModelStats(key, { days: 400 }), fetchOpportunities({ max: 300 })])
      .then(([s, o]) => { setStats(s); setOpps(o) })
      .catch(setError)
  }, [key])

  const keys = useMemo(
    () => [...new Set([...(stats || []), ...opps].map((r) => r.watchlist_key).filter(Boolean))].sort(),
    [stats, opps],
  )

  const series = useMemo(
    () => (stats || [])
      .filter((r) => !key || r.watchlist_key === key)
      .map((r) => ({
        day: String(r.day).slice(5),
        jp: r.jp_median_price_chf,
        ch: r.ch_median_price_chf,
        landed: r.median_landed_chf,
        spread: r.spread_chf,
      })),
    [stats, key],
  )

  const modelOpps = useMemo(
    () => opps.filter((o) => !key || o.watchlist_key === key),
    [opps, key],
  )

  // Days-listed distribution across the comps we actually matched: how long
  // this model really takes to move in Switzerland.
  const daysBuckets = useMemo(() => {
    const buckets = [0, 0, 0, 0, 0]
    const labels = ['0–14', '15–30', '31–60', '61–120', '120+']
    modelOpps.forEach((o) => {
      const d = o.comps?.median_days_listed
      if (d == null) return
      const i = d <= 14 ? 0 : d <= 30 ? 1 : d <= 60 ? 2 : d <= 120 ? 3 : 4
      buckets[i] += 1
    })
    return labels.map((label, i) => ({ label, count: buckets[i] }))
  }, [modelOpps])

  const latest = series[series.length - 1]
  const first = series[0]
  const spreadMove = latest && first ? (latest.spread ?? 0) - (first.spread ?? 0) : null
  const cutRate = useMemo(() => {
    const values = modelOpps.map((o) => o.comps?.pct_with_price_cut).filter((v) => v != null)
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null
  }, [modelOpps])

  if (error) return <ErrorBox error={error} />
  if (!stats) return <Loading what="model history" />

  return (
    <div className="space-y-4">
      <select className="input" value={key || ''} onChange={(e) => navigate(`/models/${e.target.value}`)}>
        <option value="">All models</option>
        {keys.map((k) => <option key={k} value={k}>{k}</option>)}
      </select>

      {series.length === 0 ? (
        <Empty>No history yet — snapshots accumulate one per day, per model.</Empty>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Stat label="JP median" value={chf(latest?.jp)} />
            <Stat label="CH median" value={chf(latest?.ch)} />
            <Stat label="Landed median" value={chf(latest?.landed)} />
            <Stat label="Spread" value={chf(latest?.spread)} tone={signClass(latest?.spread)} />
          </div>

          <Panel title="Spread over time"
                 note={spreadMove == null ? null
                   : `${spreadMove >= 0 ? 'Widened' : 'Narrowed'} by ${chf(Math.abs(spreadMove))} over the window`}>
            <ResponsiveContainer width="100%" height={230}>
              <LineChart data={series} margin={{ left: -12, right: 8, top: 8 }}>
                <CartesianGrid {...GRID} strokeDasharray="2 4" />
                <XAxis dataKey="day" {...AXIS} minTickGap={24} />
                <YAxis {...AXIS} tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
                <Tooltip {...TOOLTIP} formatter={(v) => chf(v)} />
                <Line type="monotone" dataKey="ch" name="CH median ask" stroke="#60a5fa" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="landed" name="Landed cost" stroke="#fbbf24" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="jp" name="JP ask" stroke="#9ca3af" dot={false} strokeDasharray="3 3" />
              </LineChart>
            </ResponsiveContainer>
          </Panel>

          <div className="grid gap-4 md:grid-cols-2">
            <Panel title="Comp days-listed distribution">
              <ResponsiveContainer width="100%" height={190}>
                <BarChart data={daysBuckets} margin={{ left: -20, right: 8, top: 8 }}>
                  <CartesianGrid {...GRID} strokeDasharray="2 4" />
                  <XAxis dataKey="label" {...AXIS} />
                  <YAxis {...AXIS} allowDecimals={false} />
                  <Tooltip {...TOOLTIP} />
                  <Bar dataKey="count" name="opportunities" fill="#4ade80" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </Panel>

            <Panel title="Demand signals">
              <dl className="space-y-2 text-sm">
                <Row label="Comps with a price cut" value={pct(cutRate, 0)}
                     hint="High means sellers are not getting their ask." />
                <Row label="Median days listed"
                     value={latest?.spread != null ? (stats.at(-1)?.median_days_listed ?? '—') : '—'}
                     hint="How long Swiss stock sits before it moves." />
                <Row label="Live JP listings" value={stats.at(-1)?.jp_count ?? '—'} />
                <Row label="Live CH listings" value={stats.at(-1)?.ch_count ?? '—'} />
                <Row label="Best score today" value={num(stats.at(-1)?.best_opportunity_score, 3)} />
              </dl>
            </Panel>
          </div>
        </>
      )}
    </div>
  )
}

function Panel({ title, note, children }) {
  return (
    <section className="card p-3">
      <h2 className="mb-1 text-sm font-semibold">{title}</h2>
      {note && <p className="mb-2 text-xs text-neutral-500">{note}</p>}
      {children}
    </section>
  )
}

function Row({ label, value, hint }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-edge/40 pb-1">
      <dt className="text-neutral-400">
        {label}
        {hint && <span className="block text-[11px] text-neutral-600">{hint}</span>}
      </dt>
      <dd className="tabular font-medium">{value}</dd>
    </div>
  )
}
