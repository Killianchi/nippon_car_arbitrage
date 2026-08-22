import { useEffect, useState } from 'react'
import {
  GoogleAuthProvider, onAuthStateChanged, signInWithEmailAndPassword,
  signInWithPopup, signOut,
} from 'firebase/auth'
import { auth, configured } from '../lib/firebase'

export function useAuth() {
  const [user, setUser] = useState(undefined) // undefined = still checking
  useEffect(() => {
    if (!configured) { setUser(null); return }
    return onAuthStateChanged(auth, setUser)
  }, [])
  return user
}

export function logout() {
  return signOut(auth)
}

export function SignIn() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  if (!configured) {
    return (
      <Shell>
        <p className="text-sm text-warn">
          Firebase is not configured. Set the <code>VITE_FIREBASE_*</code> variables
          (see <code>dashboard/.env.example</code>) and rebuild.
        </p>
      </Shell>
    )
  }

  const run = async (fn) => {
    setBusy(true); setError(null)
    try { await fn() }
    catch (e) { setError(e.message.replace('Firebase: ', '')) }
    finally { setBusy(false) }
  }

  return (
    <Shell>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault()
          run(() => signInWithEmailAndPassword(auth, email, password))
        }}
      >
        <input className="input" type="email" placeholder="email" autoComplete="username"
               value={email} onChange={(e) => setEmail(e.target.value)} />
        <input className="input" type="password" placeholder="password"
               autoComplete="current-password"
               value={password} onChange={(e) => setPassword(e.target.value)} />
        <button className="btn w-full" disabled={busy} type="submit">Sign in</button>
      </form>
      <div className="my-3 text-center text-xs text-neutral-600">or</div>
      <button className="btn w-full" disabled={busy}
              onClick={() => run(() => signInWithPopup(auth, new GoogleAuthProvider()))}>
        Continue with Google
      </button>
      {error && <p className="mt-3 text-sm text-neg">{error}</p>}
      <p className="mt-4 text-xs text-neutral-600">
        Access is restricted to allow-listed accounts. Signing in with any other
        account will authenticate but read nothing.
      </p>
    </Shell>
  )
}

function Shell({ children }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="card w-full max-w-sm p-6">
        <h1 className="mb-1 text-lg font-semibold">nippon-margin</h1>
        <p className="mb-5 text-xs text-neutral-500">Japan → Switzerland arbitrage</p>
        {children}
      </div>
    </div>
  )
}
