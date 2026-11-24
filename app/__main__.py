"""Start the server: python -m app"""

import sys

import uvicorn

from app.config import ConfigError, load_config
from app.main import create_app


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    missing = cfg.missing_agent_keys()
    if missing:
        print(
            "Note: calls stay off until your OpenRouter key is saved. "
            "Run: .venv/bin/python -m scripts.set_keys"
        )
    if cfg.dashboard_mode == "locked":
        print(
            "The dashboard is locked: other computers can reach Oscar and it has no password. "
            "Set one with: .venv/bin/python -m scripts.set_keys --password, then restart Oscar."
        )
    if cfg.host in ("0.0.0.0", "::"):
        print(f"Oscar dashboard: port {cfg.port}, at every address of this machine")
    else:
        print(f"Oscar dashboard: http://{cfg.host}:{cfg.port}")
    # The dashboard's live-update stream never closes on its own, so don't wait
    # on it forever when stopping (Ctrl+C).
    uvicorn.run(
        create_app(cfg),
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        # No line for every request: an open dashboard asks for news every three
        # seconds, which would fill the log of an Oscar that runs for months
        # (scripts/autostart.py).
        access_log=False,
        timeout_graceful_shutdown=3,
        # Who connected is the address that connected (or the visitor Cloudflare names, app/signin.py):
        # uvicorn would otherwise believe X-Forwarded-For from this machine, which a visitor
        # can write themselves through a proxy here, to dodge the wrong-password wait.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
