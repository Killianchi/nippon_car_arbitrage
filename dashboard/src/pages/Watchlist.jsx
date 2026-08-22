import { useEffect, useState } from 'react'
import { fetchWatchlist, saveWatchlist } from '../lib/data'
import { ErrorBox, Loading } from '../components/Common'

const BLANK = {
  key: '', make: '', model: '', aliases: [], model_codes: [],
  body: '', max_km: null, min_grade: null, homologation_mfk_chf: null,
}

const BODIES = ['', 'coupe', 'convertible', 'sedan', 'suv', 'offroad_4x4', 'hatch']

/**
 * Live watchlist editor.
 *
 * This writes `config/watchlist`, the one document the Firestore rules let a
 * client touch. The next Actions run merges it over config.yaml. Cost
 * parameters deliberately stay in the repo: a git commit is a record of what
 * you believed your tax and shipping numbers were on a given day, and nothing
 * in a browser should be able to rewrite that.
 */
export default function Watchlist({ user }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { fetchWatchlist().then(setItems).catch(setError) }, [])

  const patch = (i, field, value) =>
    setItems(items.map((it, idx) => (idx === i ? { ...it, [field]: value } : it)))

  const save = async () => {
    setBusy(true); setStatus(null)
    const cleaned = items
      .filter((it) => it.key && it.make && it.model)
      .map((it) => ({
        ...it,
        aliases: toList(it.aliases),
        model_codes: toList(it.model_codes),
        max_km: toNum(it.max_km),
        min_grade: toNum(it.min_grade),
        homologation_mfk_chf: toNum(it.homologation_mfk_chf),
        body: it.body || null,
      }))
    try {
      await saveWatchlist(cleaned, user?.uid)
      setItems(cleaned)
      setStatus('Saved. The next scheduled run picks this up.')
    } catch (e) { setError(e) }
    finally { setBusy(false) }
  }

  if (error) return <ErrorBox error={error} />
  if (!items) return <Loading what="watchlist" />

  return (
    <div className="space-y-3">
      <div className="card p-3 text-xs text-neutral-500">
        Overrides <code>config.yaml</code>'s watchlist on the next run. An empty
        list here means the repo's watchlist is used as-is. Cost parameters are
        not editable from the dashboard by design.
      </div>

      {items.map((item, i) => (
        <div key={i} className="card space-y-2 p-3">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Field label="key" value={item.key} onChange={(v) => patch(i, 'key', v)}
                   placeholder="porsche_911" />
            <Field label="make" value={item.make} onChange={(v) => patch(i, 'make', v)}
                   placeholder="Porsche" />
            <Field label="model" value={item.model} onChange={(v) => patch(i, 'model', v)}
                   placeholder="911" />
            <label className="block">
              <span className="mb-1 block text-[11px] uppercase tracking-wide text-neutral-500">body</span>
              <select className="input" value={item.body || ''}
                      onChange={(e) => patch(i, 'body', e.target.value)}>
                {BODIES.map((b) => <option key={b} value={b}>{b || '—'}</option>)}
              </select>
            </label>
          </div>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Field label="aliases (comma-sep)" value={joinList(item.aliases)}
                   onChange={(v) => patch(i, 'aliases', v)} placeholder="996, 997, Carrera" />
            <Field label="model codes" value={joinList(item.model_codes)}
                   onChange={(v) => patch(i, 'model_codes', v)} placeholder="997M9701" />
            <Field label="max km" value={item.max_km ?? ''} type="number"
                   onChange={(v) => patch(i, 'max_km', v)} />
            <Field label="min grade" value={item.min_grade ?? ''} type="number"
                   onChange={(v) => patch(i, 'min_grade', v)} placeholder="4" />
          </div>
          <div className="flex items-end gap-2">
            <Field label="homologation CHF override" value={item.homologation_mfk_chf ?? ''}
                   type="number" onChange={(v) => patch(i, 'homologation_mfk_chf', v)} />
            <button className="btn text-neg"
                    onClick={() => setItems(items.filter((_, idx) => idx !== i))}>
              Remove
            </button>
          </div>
        </div>
      ))}

      <div className="flex flex-wrap gap-2">
        <button className="btn" onClick={() => setItems([...items, { ...BLANK }])}>
          Add model
        </button>
        <button className="btn font-semibold" disabled={busy} onClick={save}>
          {busy ? 'Saving…' : 'Save watchlist'}
        </button>
      </div>
      {status && <p className="text-sm text-pos">{status}</p>}
    </div>
  )
}

function Field({ label, value, onChange, type = 'text', placeholder }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] uppercase tracking-wide text-neutral-500">{label}</span>
      <input className="input" type={type} value={value ?? ''} placeholder={placeholder}
             onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

const joinList = (v) => (Array.isArray(v) ? v.join(', ') : v || '')
const toList = (v) =>
  Array.isArray(v) ? v : String(v || '').split(',').map((s) => s.trim()).filter(Boolean)
const toNum = (v) => (v === '' || v === null || v === undefined ? null : Number(v))
