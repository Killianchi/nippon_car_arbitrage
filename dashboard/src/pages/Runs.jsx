import { useEffect, useMemo, useState } from 'react'
import { fetchRuns } from '../lib/data'
import { Chip, Empty, ErrorBox, Loading, Stat } from '../components/Common'

/**
 * Run health.
 *
 * The question this page answers: *which site changed its HTML?* A source
 * that quietly drops to zero looks fine in the digest and is invisible in the
 * opportunity table, so the per-adapter counts get their own screen with a
 * sparkline of the last runs.
 */
export default function Runs() {
  const [runs, setRuns] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => { fetchRuns({ max: 20 }).then(setRuns).catch(setError) }, [])

  const history = useMemo(() => {
    if (!runs) return {}
    const out = {}
    // Oldest first, so the trend reads left to right.
    ;[...runs].reverse().forEach((run) => {
      (run.adapters || []).forEach((a) => {
        out[a.source] = out[a.source] || []
        out[a.source].push(a)
      })
    })
    return out
  }, [runs])

  if (error) return <ErrorBox error={error} />
  if (!runs) return <Loading what="run history" />
  if (!runs.length) return <Empty>No runs recorded yet.</Empty>

  const last = runs[0]
  const okCount = (last.adapters || []).filter((a) => a.ok).length

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Last run" value={String(last.id).slice(0, 8)} />
        <Stat label="Sources ok" value={`${okCount}/${(last.adapters || []).length}`}
              tone={okCount === (last.adapters || []).length ? 'text-pos' : 'text-warn'} />
        <Stat label="JP listings" value={last.jp_count ?? 0} />
        <Stat label="CH listings" value={last.ch_count ?? 0} />
      </div>

      <section className="card p-3">
        <h2 className="mb-2 text-sm font-semibold">Per-source trend</h2>
        <p className="mb-3 text-xs text-neutral-500">
          A bar that collapses to zero and stays there means that site changed its
          HTML. The raw pages are in the run's Actions artifact.
        </p>
        <div className="space-y-3">
          {Object.entries(history).map(([source, entries]) => {
            const latest = entries[entries.length - 1]
            const peak = Math.max(...entries.map((e) => e.count), 1)
            return (
              <div key={source}>
                <div className="mb-1 flex items-baseline justify-between text-sm">
                  <span className="font-medium">{source}</span>
                  <span className="tabular text-neutral-400">
                    {latest.count} listings · {latest.duration_s?.toFixed?.(0) ?? '—'}s
                    {' '}{latest.ok ? <Chip tone="good">ok</Chip> : <Chip tone="bad">failed</Chip>}
                  </span>
                </div>
                <div className="flex h-8 items-end gap-0.5">
                  {entries.map((e, i) => (
                    <div key={i}
                         title={`${e.count} listings`}
                         className={`flex-1 rounded-sm ${e.ok ? 'bg-pos/60' : 'bg-neg/70'}`}
                         style={{ height: `${Math.max((e.count / peak) * 100, 4)}%` }} />
                  ))}
                </div>
                {latest.error && <p className="mt-1 text-xs text-neg">{latest.error}</p>}
              </div>
            )
          })}
        </div>
      </section>

      <section className="card overflow-x-auto">
        <table className="w-full">
          <thead className="border-b border-edge">
            <tr>
              <th className="th">Run</th><th className="th">Status</th>
              <th className="th">JP</th><th className="th">CH</th>
              <th className="th">Sources</th><th className="th">Errors</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id} className="border-b border-edge/50 align-top">
                <td className="td font-mono text-xs">{run.id}</td>
                <td className="td">
                  {run.ok ? <Chip tone="good">ok</Chip> : <Chip tone="bad">failed</Chip>}
                </td>
                <td className="td tabular">{run.jp_count}</td>
                <td className="td tabular">{run.ch_count}</td>
                <td className="td text-xs text-neutral-400">
                  {(run.adapters || []).map((a) => `${a.source}:${a.count}`).join('  ')}
                </td>
                <td className="td max-w-xs whitespace-normal text-xs text-neg">
                  {(run.errors || []).slice(0, 3).join('; ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
