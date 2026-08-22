// Firestore reads.
//
// Cost discipline: the dashboard never scans `listings_*`. The daily run
// precomputes `summaries/opportunities`, `summaries/last_run` and the
// per-model daily stats, so a cold open is a handful of document reads
// rather than a few thousand.
import {
  collection, doc, getDoc, getDocs, limit as fsLimit,
  orderBy, query, setDoc, where,
} from 'firebase/firestore'
import { db } from './firebase'

export async function fetchOpportunities({ max = 200 } = {}) {
  const q = query(
    collection(db, 'opportunities'),
    orderBy('opportunity_score', 'desc'),
    fsLimit(max),
  )
  const snap = await getDocs(q)
  return snap.docs.map((d) => ({ id: d.id, ...d.data() }))
}

export async function fetchSummary(name) {
  const snap = await getDoc(doc(db, 'summaries', name))
  return snap.exists() ? snap.data() : null
}

export async function fetchModelStats(watchlistKey, { days = 120 } = {}) {
  const clauses = [collection(db, 'model_stats_daily')]
  if (watchlistKey) clauses.push(where('watchlist_key', '==', watchlistKey))
  clauses.push(orderBy('day', 'desc'), fsLimit(days))
  const snap = await getDocs(query(...clauses))
  return snap.docs.map((d) => d.data()).reverse()
}

export async function fetchFx({ days = 120 } = {}) {
  const q = query(collection(db, 'fx_rates'), orderBy('day', 'desc'), fsLimit(days))
  const snap = await getDocs(q)
  return snap.docs.map((d) => d.data()).reverse()
}

export async function fetchRuns({ max = 20 } = {}) {
  const q = query(collection(db, 'runs'), orderBy('started_at', 'desc'), fsLimit(max))
  const snap = await getDocs(q)
  return snap.docs.map((d) => ({ id: d.id, ...d.data() }))
}

export async function fetchWatchlist() {
  const snap = await getDoc(doc(db, 'config', 'watchlist'))
  return snap.exists() ? snap.data().items || [] : []
}

/**
 * The only client write the rules permit. Cost parameters stay in
 * config.yaml on purpose -- nothing in a browser should be able to change a
 * tax rate.
 */
export async function saveWatchlist(items, uid) {
  await setDoc(doc(db, 'config', 'watchlist'), {
    items,
    updated_at: new Date().toISOString(),
    updated_by: uid || 'dashboard',
  })
}
