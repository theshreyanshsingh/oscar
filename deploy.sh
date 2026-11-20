#!/usr/bin/env bash
# Puts the latest Oscar from GitHub live on a server (Linux) under pm2, in one go: ./deploy.sh in this folder.
# "Run it on a server (pm2)" in README.md says how to prepare the server. (On a Mac, Oscar runs at login through
# scripts/autostart.py instead.)
#
#   1. Stops if Oscar is on a call: nothing is changed (--force goes ahead and cuts the call off).
#   2. Gets the latest code (git pull), and sets up Oscar's Python environments or installs their packages again when
#      their requirements changed: .venv, and .venv-whatsapp once WhatsApp is wanted (--whatsapp, the first time).
#   3. Checks that the new code starts. If it does not, goes back to the version before and leaves Oscar running it.
#   4. Starts Oscar with pm2 (ecosystem.config.cjs), or restarts it on the new code, and waits until it answers.

# uv and Node (for pm2) are often only on a login shell's PATH, and an SSH command's shell is not one.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v node >/dev/null 2>&1 && [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
  . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
fi

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

step() { printf '\n==> %s\n' "$*"; }
fail() {
  printf '\nDeploy stopped: %s\n' "$*" >&2
  exit 1
}

FORCE=""
WHATSAPP=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --whatsapp) WHATSAPP=1 ;;
    *) fail "unknown option $arg (the options are --whatsapp and --force)." ;;
  esac
done

PLIST="$HOME/Library/LaunchAgents/local.oscar.server.plist"
if [ -f "$PLIST" ] && grep -q "$ROOT/.venv/bin/python" "$PLIST"; then
  fail "on this Mac Oscar already runs at login. Update it with: git pull && .venv/bin/python -m scripts.autostart --restart"
fi

command -v git >/dev/null 2>&1 || fail "git is not installed here."
if ! command -v pm2 >/dev/null 2>&1; then
  command -v npm >/dev/null 2>&1 || fail "pm2 needs Node.js. Install both with: sudo apt install -y nodejs npm && sudo npm install -g pm2"
  step "Installing pm2"
  npm install -g pm2 || fail "pm2 could not be installed. Install it with: sudo npm install -g pm2"
fi
if command -v uv >/dev/null 2>&1; then
  new_env() { uv venv --quiet --python 3.13 "$1"; }
  install() { uv pip install --quiet --python "$1/bin/python" -r "$2"; }
elif command -v python3.13 >/dev/null 2>&1; then
  new_env() { python3.13 -m venv "$1"; }
  install() { "$1/bin/pip" install --quiet -r "$2"; }
else
  fail "Oscar needs Python 3.13. Install uv, which brings it: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# One deploy at a time: a second one while one runs stops here.
LOCK="$ROOT/.deploy.lock"
mkdir "$LOCK" 2>/dev/null || fail "another deploy is running. (If none is, one was cut short: remove $LOCK and try again.)"
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# Asks the running Oscar, the way scripts/autostart.py does: "calls" (how many it is on) or "answering" (1 or 0).
ask_oscar() {
  [ -x .venv/bin/python ] || {
    echo 0
    return
  }
  .venv/bin/python -c "
import sys
from app.config import load_config
from scripts.autostart import calls_in_progress, is_answering
cfg = load_config()
print(calls_in_progress(cfg) if sys.argv[1] == 'calls' else int(is_answering(cfg)))
" "$1" 2>/dev/null || echo 0
}

if [ -z "$FORCE" ] && [ "$(ask_oscar calls)" != 0 ]; then
  fail "Oscar is on a call, so nothing was changed. Deploy again once it ends, or add --force to cut it off."
fi

step "Getting the latest code"
before="$(git rev-parse HEAD)"
git pull --ff-only || fail "git pull did not work, so nothing was changed. Run git status here: a change made on this server may be in the way."
after="$(git rev-parse HEAD)"
git log -1 --format='%h %s' | cut -c1-110

# An environment is set up once; its packages are installed again only when its requirements file changed.
prepare() {
  local env="$1" requirements="$2" want
  want="$(cksum < "$requirements")"
  if [ ! -x "$env/bin/python" ]; then
    step "Setting up $env (Python 3.13)"
    new_env "$env"
  fi
  if [ "$(cat "$env/.oscar-requirements" 2>/dev/null)" != "$want" ]; then
    step "Installing the packages in $requirements"
    install "$env" "$requirements"
    printf '%s\n' "$want" > "$env/.oscar-requirements"
  fi
}
prepare .venv requirements.txt
if [ -n "$WHATSAPP" ] || [ -x .venv-whatsapp/bin/python ]; then
  prepare .venv-whatsapp requirements-whatsapp.txt
fi

step "Checking that the new code starts"
# (Its settings are read too: Oscar refuses to start with settings that do not go together.)
problem=""
if ! out="$(.venv/bin/python -c 'import app.main; from app.config import load_config; load_config()' 2>&1)"; then
  problem="$out"
elif [ -x .venv-whatsapp/bin/python ] && ! out="$(.venv-whatsapp/bin/python -c 'import app.whatsapp.bridge' 2>&1)"; then
  problem="$out"
  case "$out" in *libmagic*) problem="$out

WhatsApp needs the libmagic library: sudo apt install -y libmagic1" ;; esac
fi
if [ -n "$problem" ]; then
  printf '%s\n' "$problem" | tail -n 25
  if [ "$before" != "$after" ]; then
    git reset --quiet --keep "$before" || fail "the new code does not start (the error is above), and the code before could not be put back: run git status here. Oscar was not restarted."
    fail "the new code does not start (the error is above), so Oscar was left running the version before."
  fi
  fail "Oscar does not start (the error is above)."
fi
echo "It does."

step "Starting Oscar with pm2"
APPS=(oscar)
[ -x .venv-whatsapp/bin/python ] && APPS+=(oscar-whatsapp)
# pm2's list of processes as JSON ([] when it has none, or cannot say).
processes() { pm2 jlist 2>/dev/null || echo "[]"; }
# Oscar started under pm2 by hand from this folder under other names holds the same port: it is replaced, once.
earlier="$(processes | node -e '
  let raw = ""
  process.stdin.on("data", (chunk) => (raw += chunk)).on("end", () => {
    let list = []
    try { list = JSON.parse(raw.slice(raw.indexOf("["))) } catch {}
    const [root, ...ours] = process.argv.slice(1)
    for (const item of list) {
      const cwd = String(item.pm2_env?.pm_cwd || "")
      if (!ours.includes(item.name) && (cwd === root || cwd.startsWith(`${root}/`))) console.log(`${item.pm_id} ${item.name}`)
    }
  })
' "$ROOT" oscar oscar-whatsapp)"
if [ -n "$earlier" ]; then
  while read -r id name; do
    echo "Replacing pm2 process $id ($name), which ran Oscar from this folder before"
    pm2 delete "$id" >/dev/null
  done <<< "$earlier"
fi
pm2 startOrReload ecosystem.config.cjs --update-env
pm2 save >/dev/null

# Each process is up once pm2 has kept it running for 10 seconds, and Oscar once it answers too. (A process that
# keeps stopping again is not up, whatever answers on its port.)
step "Waiting for Oscar to answer"
state() {
  processes | node -e '
    let raw = ""
    process.stdin.on("data", (chunk) => (raw += chunk)).on("end", () => {
      let list = []
      try { list = JSON.parse(raw.slice(raw.indexOf("["))) } catch {}
      const item = list.find((entry) => entry.name === process.argv[1])
      const env = item?.pm2_env || {}
      console.log(env.status === "online" && Date.now() - env.pm_uptime > 10000 ? "up" : `${env.status || "not started"}, restarted ${env.restart_time || 0} times`)
    })
  ' "$1"
}
for app in "${APPS[@]}"; do
  ready=""
  for _ in $(seq 1 60); do
    if [ "$(state "$app")" = up ] && { [ "$app" != oscar ] || [ "$(ask_oscar answering)" = 1 ]; }; then
      ready=1
      break
    fi
    sleep 2
  done
  if [ -z "$ready" ]; then
    pm2 logs "$app" --lines 40 --nostream 2>&1 | tail -n 60 || true
    now="$(state "$app")"
    [ "$now" = up ] && now="it is running but does not answer" || now="pm2 says it is $now"
    fail "$app did not come up in 2 minutes ($now). Its last log lines are above."
  fi
  echo "$app is up"
done

# Housekeeping, once per server: pm2 starts Oscar again after the server restarts, and its logs are rotated so they
# never fill the disk.
if command -v systemctl >/dev/null 2>&1 && ! systemctl is-enabled --quiet "pm2-$(whoami)" 2>/dev/null; then
  if sudo -n true 2>/dev/null; then
    step "Making pm2 start Oscar when the server starts"
    sudo env PATH="$PATH" "$(command -v pm2)" startup systemd -u "$(whoami)" --hp "$HOME" >/dev/null
    pm2 save >/dev/null
  else
    echo "To have Oscar start again after the server restarts, run pm2 startup once and the command it prints."
  fi
fi
if [ ! -d "${PM2_HOME:-$HOME/.pm2}/modules/pm2-logrotate" ]; then
  step "Rotating pm2's logs (pm2-logrotate)"
  pm2 install pm2-logrotate >/dev/null 2>&1 || echo "pm2-logrotate could not be installed, so pm2's logs are kept whole."
fi

step "Done: commit $(git log -1 --format='%h') is live"
[ -f .env ] || echo "Oscar has no keys yet: run .venv/bin/python -m scripts.set_keys, then pm2 restart oscar."
# Reachable from other machines (HOST=0.0.0.0): browser calls also need their UDP ports let in, and without
# Cloudflare Access anyone who reaches the port can use Oscar.
.venv/bin/python -c '
from app.config import browser_call_udp_ports, load_config
cfg = load_config()
if not cfg.local_only:
    print("Browser calls send their audio straight to this server, not through its web address: let in UDP %d-%d in the firewall (on EC2, the security group)." % browser_call_udp_ports())
    if cfg.access_required:
        print("The dashboard only opens for people signed in through Cloudflare Access (%s)." % cfg.access_team_domain)
    else:
        print("WARNING: anyone who can reach this server on port %d can use Oscar, your OpenRouter credit and its call history. Put Cloudflare Access in front of it: README.md, Protect the dashboard." % cfg.port)
' 2>/dev/null || true
if [ -x .venv-whatsapp/bin/python ] && [ ! -f data/whatsapp-session.sqlite3 ]; then
  echo "WhatsApp is not linked yet: link it from the dashboard's WhatsApp card (README.md, Set up WhatsApp)."
fi
pm2 ls
