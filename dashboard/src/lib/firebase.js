// Firebase client wiring.
//
// The config below is public by design -- it names the project, it does not
// grant anything. Every read is gated by Firebase Auth plus the rules in
// firestore.rules, which allow-list a single UID and deny all client writes
// except the watchlist editor doc.
import { initializeApp } from 'firebase/app'
import { getAuth } from 'firebase/auth'
import { getFirestore } from 'firebase/firestore'

const config = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
}

export const configured = Boolean(config.apiKey && config.projectId)

const app = configured ? initializeApp(config) : null
export const auth = app ? getAuth(app) : null
export const db = app ? getFirestore(app) : null
