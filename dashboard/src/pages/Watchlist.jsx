import { useEffect, useMemo, useState } from 'react'
import { fetchWatchlist } from '../lib/data'
import { ErrorBox, Loading } from '../components/Common'

const REPO = import.meta.env.VITE_REPO_SLUG || 'Killianchi/nippon_car_arbitrage'
const EDIT_URL = `https://github.com/${REPO}/edit/main/config.yaml`

const BLANK = {
  key: '', make: '', model: '', aliases: [], model_codes: [],
  body: '', ch_model_slug: '', max_km: null, min_grade: null,
  homologation_mfk_chf: null, risk_notes: [],
}

const BODIES = ['', 'coupe', 'convertible', 'sedan', 'suv', 'offroad_4x4', 'hatch']

/**
 * Watchlist builder.
 *
 * There is no database to write to any more, and that turns out to be the
 * better design: the watchlist lives in `config.yaml`, so changing it is a
 * commit, and you get a dated history of what you were hunting and why. This
 * page does the fiddly part -- getting the YAML shape and the alias/model-code
 * fields right -- and hands you something to paste.
 */
export default function Watchlist() {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    fetchWatchlist()
      .then((rows) => setItems(rows.map((r) => ({ ...BLANK, ...r }))))
      .catch(setError)
  }, [])

  const yaml = useMemo(() => (items ? toYaml(items) : ''), [items])

  const patch = (i, field, value) =>
    setItems(items.map((it, idx) => (idx === i ? { ...it, [field]: value } : it)))

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(yaml)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  if (error) return <ErrorBox error={error} />
  if (!items) return <Loading what="watchlist" />

  return (
    <div className="space-y-3">
      <div className="card p-3 text-xs text-neutral-500">
        Edit here, then paste the generated YAML over the <code>watchlist:</code>
        {' '}block in <code>config.yaml</code>. The next scheduled run picks it up.
        Cost parameters live in the same file and are deliberately not editable here.
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
          <div className="grid grid-cols-2 items-end gap-2 md:grid-cols-4">
            <Field label="homologation CHF" value={item.homologation_mfk_chf ?? ''}
                   type="number" onChange={(v) => patch(i, 'homologation_mfk_chf', v)} />
            <Field label="Swiss URL slug" value={item.ch_model_slug || ''}
                   onChange={(v) => patch(i, 'ch_model_slug', v)} placeholder="sl-class" />
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
        <button className="btn font-semibold" onClick={copy}>
          {copied ? 'Copied ✓' : 'Copy YAML'}
        </button>
        <a className="btn" href={EDIT_URL} target="_blank" rel="noreferrer">
          Edit config.yaml on GitHub
        </a>
      </div>

      <pre className="card overflow-x-auto p-3 text-xs leading-relaxed text-neutral-300">
{yaml}
      </pre>
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

/** Quote anything YAML would otherwise read as a number or a boolean. */
function scalar(value) {
  const s = String(value)
  return /^[A-Za-z][\w\-. ]*$/.test(s) && !/^(y|n|yes|no|true|false|on|off|null)$/i.test(s)
    ? s
    : JSON.stringify(s)
}

function toYaml(items) {
  const lines = ['watchlist:']
  items
    .filter((it) => it.key && it.make && it.model)
    .forEach((it) => {
      lines.push(`  - key: ${scalar(it.key)}`)
      lines.push(`    make: ${scalar(it.make)}`)
      lines.push(`    model: ${scalar(it.model)}`)
      const aliases = toList(it.aliases)
      if (aliases.length) lines.push(`    aliases: [${aliases.map(scalar).join(', ')}]`)
      const codes = toList(it.model_codes)
      if (codes.length) lines.push(`    model_codes: [${codes.map(scalar).join(', ')}]`)
      if (it.body) lines.push(`    body: ${it.body}`)
      if (it.ch_model_slug) lines.push(`    ch_model_slug: ${scalar(it.ch_model_slug)}`)
      if (it.max_km) lines.push(`    max_km: ${Number(it.max_km)}`)
      if (it.min_grade) lines.push(`    min_grade: ${Number(it.min_grade)}`)
      if (it.homologation_mfk_chf) {
        lines.push(`    homologation_mfk_chf: ${Number(it.homologation_mfk_chf)}`)
      }
      const notes = toList(it.risk_notes)
      if (notes.length) {
        lines.push('    risk_notes:')
        notes.forEach((n) => lines.push(`      - ${scalar(n)}`))
      }
    })
  return lines.join('\n')
}
