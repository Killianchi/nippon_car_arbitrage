import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchOpportunities } from '../lib/data'
import { Chip, Empty, ErrorBox, Loading } from '../components/Common'
import { carName, chf, km, num, pct, signClass, usd } from '../lib/format'

const SORTS = [
  { key: 'opportunity_score', label: 'Score' },
  { key: 'margin_pct', label: 'Margin %' },
  { key: 'gross_margin_chf', label: 'Margin CHF' },
  { key: 'liquidity_score', label: 'Liquidity' },
  { key: 'landed', label: 'Capital' },
]

const TIERS = ['all', 'small', 'mid', 'large']

export default function Opportunities() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  const [sort, setSort] = useState('opportunity_score')
  const [tier, setTier] = useState('all')
  const [q, setQ] = useState('')
  const [hideRisky, setHideRisky] = useState(false)
  const [open, setOpen] = useState(null)

  useEffect(() => {
    fetchOpportunities({ max: 300 }).then(setRows).catch(setError)
  }, [])

  const view = useMemo(() => {
    if (!rows) return []
    const landed = (o) => o.landed_roro?.landed_chf ?? Infinity
    return rows
      .filter((o) => o.is_cheapest_duplicate !== false)
      .filter((o) => (o.opportunity_score || 0) > 0)
      .filter((o) => tier === 'all' || o.capital_tier === tier)
      .filter((o) => !hideRisky || (o.risk_flags || []).length === 0)
      .filter((o) => !q || carName(o).toLowerCase().includes(q.toLowerCase()))
      .sort((a, b) =>
        sort === 'landed' ? landed(a) - landed(b) : (b[sort] || 0) - (a[sort] || 0),
      )
  }, [rows, sort, tier, q, hideRisky])

  if (error) return <ErrorBox error={error} />
  if (!rows) return <Loading what="opportunities" />

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <input className="input flex-1 min-w-[9rem]" placeholder="filter by model…"
               value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="input w-auto" value={sort} onChange={(e) => setSort(e.target.value)}>
          {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        {TIERS.map((t) => (
          <button key={t} onClick={() => setTier(t)}
                  className={`chip border border-edge ${
                    tier === t ? 'bg-neutral-200 text-ink-900' : 'bg-ink-700 text-neutral-400'}`}>
            {t}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-1.5 text-neutral-500">
          <input type="checkbox" checked={hideRisky}
                 onChange={(e) => setHideRisky(e.target.checked)} />
          clean only
        </label>
      </div>

      <p className="text-xs text-neutral-500">
        {view.length} of {rows.length} scored · ranked by margin per franc per expected day
      </p>

      {view.length === 0 ? (
        <Empty>Nothing clears the bar right now.</Empty>
      ) : (
        <>
          {/* Mobile: stacked cards. Desktop: a real table. */}
          <div className="space-y-2 md:hidden">
            {view.map((o) => (
              <MobileCard key={o.id} o={o}
                          open={open === o.id}
                          onToggle={() => setOpen(open === o.id ? null : o.id)} />
            ))}
          </div>
          <div className="hidden md:block card overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-edge">
                <tr>
                  <th className="th">Car</th><th className="th">JP</th>
                  <th className="th">Landed RoRo</th><th className="th">Container</th>
                  <th className="th">CH p25</th><th className="th">Median</th>
                  <th className="th">Margin</th><th className="th">%</th>
                  <th className="th">Liq</th><th className="th">Score</th>
                  <th className="th">Tier</th><th className="th">Risk</th>
                </tr>
              </thead>
              <tbody>
                {view.map((o) => (
                  <Row key={o.id} o={o}
                       open={open === o.id}
                       onToggle={() => setOpen(open === o.id ? null : o.id)} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

function Row({ o, open, onToggle }) {
  return (
    <>
      <tr className="cursor-pointer border-b border-edge/60 hover:bg-ink-700/40" onClick={onToggle}>
        <td className="td font-medium">{carName(o)}</td>
        <td className="td tabular text-neutral-400">{usd(o.price_usd)}</td>
        <td className="td tabular">{chf(o.landed_roro?.landed_chf)}</td>
        <td className="td tabular text-neutral-400">{chf(o.landed_container?.landed_chf)}</td>
        <td className="td tabular">{chf(o.comps?.swiss_p25)}</td>
        <td className="td tabular text-neutral-400">{chf(o.comps?.swiss_median_ask)}</td>
        <td className={`td tabular ${signClass(o.gross_margin_chf)}`}>{chf(o.gross_margin_chf)}</td>
        <td className={`td tabular ${signClass(o.margin_pct)}`}>{pct(o.margin_pct, 0)}</td>
        <td className="td tabular">{num(o.liquidity_score)}</td>
        <td className="td tabular font-semibold text-pos">{num(o.opportunity_score, 3)}</td>
        <td className="td"><Chip>{o.capital_tier}</Chip></td>
        <td className="td">
          {(o.risk_flags || []).length > 0
            ? <Chip tone="warn">{o.risk_flags.length}</Chip>
            : <Chip tone="good">clean</Chip>}
        </td>
      </tr>
      {open && (
        <tr><td colSpan={12} className="bg-ink-900/60 px-3 py-4"><Detail o={o} /></td></tr>
      )}
    </>
  )
}

function MobileCard({ o, open, onToggle }) {
  return (
    <div className="card p-3">
      <button className="w-full text-left" onClick={onToggle}>
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-medium">{carName(o)}</span>
          <span className="tabular font-semibold text-pos">{num(o.opportunity_score, 3)}</span>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-neutral-400">
          <span className="tabular">{usd(o.price_usd)} JP</span>
          <span className="tabular">→ {chf(o.landed_roro?.landed_chf)} landed</span>
          <span className={`tabular ${signClass(o.gross_margin_chf)}`}>
            {chf(o.gross_margin_chf)} ({pct(o.margin_pct, 0)})
          </span>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Chip>{o.capital_tier}</Chip>
          <Chip>{o.comps?.comp_count ?? 0} comps</Chip>
          <Chip>liq {num(o.liquidity_score)}</Chip>
          {(o.risk_flags || []).length > 0 && <Chip tone="warn">{o.risk_flags.length} risks</Chip>}
        </div>
      </button>
      {open && <div className="mt-3 border-t border-edge pt-3"><Detail o={o} /></div>}
    </div>
  )
}

function Detail({ o }) {
  const rows = [
    ['FOB (CHF)', o.landed_roro?.fob_chf],
    ['Freight', o.landed_roro?.freight_chf],
    ['Marine insurance', o.landed_roro?.insurance_chf],
    ['= CIF', o.landed_roro?.cif_chf],
    ['Customs duty', o.landed_roro?.customs_duty_chf],
    ['Automobilsteuer 4%', o.landed_roro?.automobilsteuer_chf],
    ['VAT 8.1%', o.landed_roro?.vat_chf],
    ['Homologation / MFK', o.landed_roro?.homologation_mfk_chf],
    ['Agent + recon', o.landed_roro?.agent_recon_buffer_chf],
    ['= Landed', o.landed_roro?.landed_chf],
  ]
  return (
    <div className="grid gap-4 md:grid-cols-3">
      <div>
        <h3 className="mb-1 text-xs uppercase tracking-wide text-neutral-500">Cost breakdown (RoRo)</h3>
        <table className="w-full text-sm">
          <tbody>
            {rows.map(([label, value]) => (
              <tr key={label} className="border-b border-edge/40">
                <td className="py-1 text-neutral-400">{label}</td>
                <td className="tabular py-1 text-right">{chf(value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-neutral-500">
          Container scenario lands at {chf(o.landed_container?.landed_chf)}
          {' '}(3-car consolidation).
        </p>
        {(o.landed_roro?.notes || []).map((n) => (
          <p key={n} className="mt-1 text-xs text-warn">{n}</p>
        ))}
      </div>

      <div className="space-y-2">
        <h3 className="text-xs uppercase tracking-wide text-neutral-500">Swiss comparables</h3>
        <p className="text-sm">
          p25 {chf(o.comps?.swiss_p25)} · median {chf(o.comps?.swiss_median_ask)} ·
          p75 {chf(o.comps?.swiss_p75)}
        </p>
        <p className="text-sm text-neutral-400">
          {o.comps?.comp_count ?? 0} comps · median {o.comps?.median_days_listed ?? '—'} days listed ·
          {' '}{pct(o.comps?.pct_with_price_cut, 0)} with a price cut
        </p>
        <p className="text-sm">
          Net of capital {chf(o.net_margin_chf)} over {o.expected_holding_days} days
        </p>
        <p className="text-xs text-neutral-500">
          score = margin {pct(o.margin_pct, 0)} × liquidity {num(o.liquidity_score)} ÷
          capital weight {num(o.capital_weight)}
          {o.seasonality_multiplier !== 1 && ` × season ${num(o.seasonality_multiplier)}`}
          {o.risk_multiplier !== 1 && ` × risk ${num(o.risk_multiplier)}`}
        </p>
        <ul className="max-h-32 space-y-0.5 overflow-y-auto text-xs">
          {(o.comps?.comp_urls || []).slice(0, 10).map((u) => (
            <li key={u}>
              <a className="text-blue-400 hover:underline" href={u} target="_blank" rel="noreferrer">
                {u.replace(/^https?:\/\//, '').slice(0, 52)}…
              </a>
            </li>
          ))}
        </ul>
      </div>

      <div className="space-y-2">
        <h3 className="text-xs uppercase tracking-wide text-neutral-500">Listing</h3>
        {o.image_urls?.[0] && (
          <img src={o.image_urls[0]} alt="" loading="lazy"
               className="w-full rounded-lg border border-edge object-cover" />
        )}
        <p className="text-sm text-neutral-400">{km(o.mileage_km)}</p>
        {(o.risk_flags || []).length > 0 && (
          <ul className="space-y-1 text-xs text-warn">
            {o.risk_flags.map((f) => <li key={f}>⚠ {f}</li>)}
          </ul>
        )}
        <div className="flex flex-wrap gap-2 pt-1">
          <a className="btn" href={o.url} target="_blank" rel="noreferrer">Japanese listing</a>
          {o.watchlist_key && (
            <Link className="btn" to={`/models/${o.watchlist_key}`}>Model detail</Link>
          )}
        </div>
      </div>
    </div>
  )
}
