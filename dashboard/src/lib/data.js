// Data access: one static JSON snapshot, fetched once and memoised.
//
// The daily run writes `data.json` next to the bundle (`nippon-margin export`).
// There is no client SDK, no per-read billing, and no auth code here --
// Cloudflare Access sits in front of the whole site and is what keeps this
// private. The data is exactly as fresh as the last run, which is honest:
// it only changes once a day.

let cache = null

async function snapshot() {
  if (!cache) {
    cache = fetch(`${import.meta.env.BASE_URL}data.json`, { cache: 'no-cache' })
      .then((r) => {
        if (!r.ok) throw new Error(`data.json returned ${r.status}`)
        return r.json()
      })
      .catch((e) => {
        cache = null // let a retry actually retry
        throw e
      })
  }
  return cache
}

export async function fetchOpportunities() {
  return (await snapshot()).opportunities || []
}

export async function fetchModelStats(watchlistKey) {
  const rows = (await snapshot()).model_stats || []
  return watchlistKey ? rows.filter((r) => r.watchlist_key === watchlistKey) : rows
}

export async function fetchFx() {
  return (await snapshot()).fx || []
}

export async function fetchFxMeta() {
  const data = await snapshot()
  return { moves: data.fx_moves || {}, impacts: data.fx_impacts || [] }
}

export async function fetchRuns() {
  return (await snapshot()).runs || []
}

export async function fetchWatchlist() {
  return (await snapshot()).watchlist || []
}

export async function fetchGeneratedAt() {
  return (await snapshot()).generated_at || null
}
