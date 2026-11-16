// How pm2 runs Oscar on a server (Linux): the dashboard and calls (python -m app) and, once it is set up, the
// WhatsApp connection (python -m app.whatsapp), each started again if it stops. deploy.sh starts them and brings them
// up to date; "Run it on a server (pm2)" in README.md says how. (On a Mac, scripts/autostart.py runs Oscar instead.)
//
// One process each: Oscar keeps its calls in memory and holds its port, and two WhatsApp connections would both
// answer in a group.
const fs = require("node:fs")
const path = require("node:path")

const root = __dirname
const python = (env) => path.join(root, env, "bin", "python")

// Started again after a crash, a little later each time it crashes straight away, and given 8 seconds to finish when
// stopped (a call is closed in 3). Python's output reaches the log as it comes, and log lines are timed.
const always = {
  cwd: root,
  interpreter: "none",
  exec_mode: "fork",
  instances: 1,
  autorestart: true,
  min_uptime: "20s",
  max_restarts: 15,
  exp_backoff_restart_delay: 500,
  kill_timeout: 8000,
  time: true,
  env: { PYTHONUNBUFFERED: "1" },
}

const apps = [{ name: "oscar", script: python(".venv"), args: "-m app", ...always }]
// WhatsApp, once its own environment is set up (./deploy.sh --whatsapp, the first time).
if (fs.existsSync(python(".venv-whatsapp"))) {
  apps.push({ name: "oscar-whatsapp", script: python(".venv-whatsapp"), args: "-m app.whatsapp", ...always })
}

module.exports = { apps }
