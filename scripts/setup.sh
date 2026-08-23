#!/usr/bin/env bash
#
# One-shot setup for nippon-margin.
#
# Does everything that can be done from a terminal: sets the Telegram secrets
# (looking your chat id up for you) and, if you want it, generates and stores
# a catalog encryption key.
#
# Needs one CLI you are already signed in to:
#   gh        https://cli.github.com          (gh auth login)
#
# Re-running is safe: no existing secret is ever overwritten, and the catalog
# encryption key is never regenerated.
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
bold "1. Catalog encryption (optional)"
if secret_exists DATA_ENCRYPTION_KEY; then
  ok "DATA_ENCRYPTION_KEY already set — the catalog will be encrypted"
  warn "Not regenerating it: a new key makes the stored catalog unreadable."
else
  echo "  The catalog is committed to this repository. By default it is stored"
  echo "  as plain gzip: no secret to manage, and you can read it with"
  echo "  'gunzip' and 'sqlite3'. Encrypting it buys privacy and nothing else"
  echo "  here — git already checksums the blob."
  echo
  echo "  Encrypt it? Say yes if the repository is public and you would rather"
  echo "  your margins and deal flow were not readable by anyone who clones it."
  read -rp "  Encrypt the catalog? [y/N] " ENCRYPT
  if [[ "${ENCRYPT:-}" =~ ^[Yy] ]]; then
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
  else
    ok "catalog will be stored unencrypted — you can turn this on later by"
    ok "  setting DATA_ENCRYPTION_KEY; the next push upgrades automatically"
  fi
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
bold "3. Dashboard"
echo "  The dashboard publishes to GitHub Pages straight from the workflow."
echo "  Nothing to configure here: the first run turns Pages on and deploys to"
echo "    https://$(echo "$REPO" | cut -d/ -f1 | tr 'A-Z' 'a-z').github.io/$(echo "$REPO" | cut -d/ -f2)/"
echo
warn "That URL is public. The page carries a noindex header so it stays out of"
warn "search results, but anyone who knows the address can read your"
warn "opportunities and margins. Move it behind a gate before that matters."

# ---------------------------------------------------------------------------
bold ""
bold "Done. Current secrets:"
gh secret list --repo "$REPO" | sed 's/^/  /'
bold ""
echo "Next:  gh workflow run daily.yml --repo $REPO"
