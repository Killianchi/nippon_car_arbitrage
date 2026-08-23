#!/usr/bin/env bash
#
# One-shot setup for nippon-margin.
#
# Does everything that can be done from a terminal: generates the catalog
# encryption key, sets every GitHub Actions secret, creates the Cloudflare
# Pages project, and locks it behind a Cloudflare Access policy for your
# email only.
#
# Needs two CLIs you are already signed in to:
#   gh        https://cli.github.com          (gh auth login)
#   wrangler  npm install -g wrangler         (wrangler login)
#
# Re-running is safe: existing secrets are only replaced if you say so, and
# the Pages project and Access policy are created only if missing.
#
# Usage:  ./scripts/setup.sh [--repo owner/name] [--project nippon-margin]

set -euo pipefail

REPO=""
PROJECT="nippon-margin"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    -h|--help) sed -n "2,18p" "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is not installed. $2"; }

# ---------------------------------------------------------------------------
bold "Checking prerequisites"
need gh "Install it from https://cli.github.com, then run: gh auth login"
gh auth status >/dev/null 2>&1 || die "gh is installed but not signed in. Run: gh auth login"
ok "gh authenticated as $(gh api user --jq .login)"

if [[ -z "$REPO" ]]; then
  REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
  [[ -n "$REPO" ]] || die "Could not detect the repository. Pass --repo owner/name."
fi
ok "repository: $REPO"

# ---------------------------------------------------------------------------
# A secret is only worth setting once; clobbering DATA_ENCRYPTION_KEY would
# make every previously stored catalog unreadable, so it is guarded hard.
secret_exists() { gh secret list --repo "$REPO" 2>/dev/null | awk '{print $1}' | grep -qx "$1"; }

set_secret() {
  local name="$1" value="$2"
  printf '%s' "$value" | gh secret set "$name" --repo "$REPO" --body-file - >/dev/null
  ok "set $name"
}

prompt_secret() {   # name, human description, is_sensitive
  local name="$1" desc="$2" value=""
  if secret_exists "$name"; then
    ok "$name already set (leaving it alone)"
    return
  fi
  printf '  %s\n    ' "$desc"
  read -rs value; echo
  if [[ -z "$value" ]]; then
    warn "skipped $name — set it later with: gh secret set $name --repo $REPO"
    return
  fi
  set_secret "$name" "$value"
}

# ---------------------------------------------------------------------------
bold ""
bold "1. Catalog encryption key"
if secret_exists DATA_ENCRYPTION_KEY; then
  ok "DATA_ENCRYPTION_KEY already set"
  warn "Not regenerating it: a new key makes the stored catalog unreadable."
else
  # Generated here rather than handed to you over a chat or an email, so the
  # only copies are this terminal and the GitHub secret store.
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  set_secret DATA_ENCRYPTION_KEY "$KEY"
  echo
  bold "  Save this somewhere safe — it is shown once:"
  echo "    $KEY"
  echo
  echo "  Losing it does not break the scraper, but the stored catalog becomes"
  echo "  unreadable: every first_seen date and price-history point is gone."
  echo "  You also need it locally for: nippon-margin sync pull"
  echo
  read -rp "  Press enter once you have saved it. " _
fi

# ---------------------------------------------------------------------------
bold ""
bold "2. Telegram"
prompt_secret TELEGRAM_BOT_TOKEN "Bot token from @BotFather (/newbot). Input is hidden:"

if secret_exists TELEGRAM_CHAT_ID; then
  ok "TELEGRAM_CHAT_ID already set"
elif [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || secret_exists TELEGRAM_BOT_TOKEN; then
  echo "  Send your bot any message now, then press enter to look up the chat id."
  read -rp "  " _
  echo "  Paste the bot token once more so I can call getUpdates (not stored):"
  read -rs TOKEN_FOR_LOOKUP; echo
  if [[ -n "$TOKEN_FOR_LOOKUP" ]]; then
    CHAT_ID="$(curl -sS "https://api.telegram.org/bot${TOKEN_FOR_LOOKUP}/getUpdates" \
      | python3 -c 'import json,sys
try:
    updates = json.load(sys.stdin).get("result", [])
    ids = [u[k]["chat"]["id"] for u in updates for k in ("message","channel_post") if k in u]
    print(ids[-1] if ids else "")
except Exception:
    print("")' )"
    if [[ -n "$CHAT_ID" ]]; then
      set_secret TELEGRAM_CHAT_ID "$CHAT_ID"
      ok "found chat id $CHAT_ID"
    else
      warn "No messages found. Message the bot, then run:"
      warn "  gh secret set TELEGRAM_CHAT_ID --repo $REPO"
    fi
  fi
fi

# ---------------------------------------------------------------------------
bold ""
bold "3. Cloudflare Pages"
if ! command -v wrangler >/dev/null 2>&1; then
  warn "wrangler not installed — skipping. Install with: npm install -g wrangler"
else
  if wrangler pages project list 2>/dev/null | grep -q "\b${PROJECT}\b"; then
    ok "Pages project '$PROJECT' already exists"
  else
    wrangler pages project create "$PROJECT" --production-branch=main >/dev/null \
      && ok "created Pages project '$PROJECT'" \
      || warn "could not create the Pages project — create it in the dashboard"
  fi
fi

prompt_secret CLOUDFLARE_API_TOKEN \
  "Cloudflare API token (My Profile → API Tokens → Create → 'Cloudflare Pages: Edit'.
    Add the 'Access: Apps and Policies: Edit' permission too if you want this
    script to configure the Access policy for you). Input is hidden:"
prompt_secret CLOUDFLARE_ACCOUNT_ID \
  "Cloudflare account id (Workers & Pages → the ID in the sidebar):"

# ---------------------------------------------------------------------------
bold ""
bold "4. Cloudflare Access (this is what keeps the dashboard private)"
echo "  Without a policy, '${PROJECT}.pages.dev' is a public URL with your"
echo "  entire deal flow on it. Configure it now?"
read -rp "  Cloudflare API token with Access:Edit (blank to do it in the dashboard): " -s CF_TOKEN; echo
read -rp "  Cloudflare account id (blank to skip): " CF_ACCOUNT
read -rp "  Your email address (the only one allowed in): " CF_EMAIL

if [[ -n "$CF_TOKEN" && -n "$CF_ACCOUNT" && -n "$CF_EMAIL" ]]; then
  API="https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT}/access/apps"
  APP_ID="$(curl -sS -H "Authorization: Bearer $CF_TOKEN" "$API" \
    | python3 -c "import json,sys
try:
    apps = json.load(sys.stdin).get('result') or []
    print(next((a['id'] for a in apps if '${PROJECT}' in (a.get('domain') or '')), ''))
except Exception:
    print('')")"

  if [[ -z "$APP_ID" ]]; then
    APP_ID="$(curl -sS -X POST "$API" \
      -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
      -d "{\"name\":\"nippon-margin dashboard\",
           \"domain\":\"${PROJECT}.pages.dev\",
           \"type\":\"self_hosted\",
           \"session_duration\":\"720h\"}" \
      | python3 -c "import json,sys
try:
    print((json.load(sys.stdin).get('result') or {}).get('id',''))
except Exception:
    print('')")"
    [[ -n "$APP_ID" ]] && ok "created Access application" || warn "could not create the Access application"
  else
    ok "Access application already exists"
  fi

  if [[ -n "$APP_ID" ]]; then
    curl -sS -X POST "${API}/${APP_ID}/policies" \
      -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
      -d "{\"name\":\"owner only\",\"decision\":\"allow\",\"precedence\":1,
           \"include\":[{\"email\":{\"email\":\"${CF_EMAIL}\"}}]}" >/dev/null \
      && ok "Access policy allows ${CF_EMAIL} only" \
      || warn "could not create the Access policy — add it in the dashboard"
  fi
else
  warn "Skipped. Zero Trust → Access → Applications → Add → Self-hosted:"
  warn "  domain ${PROJECT}.pages.dev, policy Allow → Emails → your address."
fi

# ---------------------------------------------------------------------------
bold ""
bold "Done. Current secrets:"
gh secret list --repo "$REPO" | sed 's/^/  /'
bold ""
echo "Next:  gh workflow run daily.yml --repo $REPO"
echo "       (the workflow must be on the default branch first)"
