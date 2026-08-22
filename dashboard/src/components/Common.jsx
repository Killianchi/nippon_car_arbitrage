export function Loading({ what = 'data' }) {
  return <p className="py-10 text-center text-sm text-neutral-500">Loading {what}…</p>
}

export function ErrorBox({ error }) {
  const denied = /permission|insufficient/i.test(String(error?.message || error))
  return (
    <div className="card p-4">
      <p className="text-sm text-neg">{String(error?.message || error)}</p>
      {denied && (
        <p className="mt-2 text-xs text-neutral-500">
          The rules allow-list your Firebase UID explicitly. Put your UID into
          <code className="mx-1">allowedUids()</code> in <code>firestore.rules</code>
          and redeploy with <code>firebase deploy --only firestore:rules</code>.
        </p>
      )}
    </div>
  )
}

export function Empty({ children }) {
  return <p className="py-10 text-center text-sm text-neutral-500">{children}</p>
}

export function Chip({ tone = 'neutral', children }) {
  const tones = {
    neutral: 'bg-ink-600 text-neutral-300',
    good: 'bg-pos/15 text-pos',
    bad: 'bg-neg/15 text-neg',
    warn: 'bg-warn/15 text-warn',
  }
  return <span className={`chip ${tones[tone]}`}>{children}</span>
}

export function Stat({ label, value, tone }) {
  return (
    <div className="card px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`tabular text-base font-semibold ${tone || ''}`}>{value}</div>
    </div>
  )
}
