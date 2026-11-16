"""Start Oscar's WhatsApp connection: .venv-whatsapp/bin/python -m app.whatsapp

Add --verbose to log every message that arrives (its kind and whether it was
for Oscar, never its words), for finding out why Oscar did not answer.

Run it with --unlink to sign Oscar out of the WhatsApp account it is linked to,
for example after the wrong phone scanned the code.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys

from app.config import ConfigError, load_config
from app.store import Store


async def _run(bridge) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await bridge.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Oscar's WhatsApp connection.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every message that arrives (never its words) and what Oscar made of it",
    )
    parser.add_argument(
        "--unlink",
        action="store_true",
        help="sign Oscar out of the WhatsApp account it is linked to, then exit",
    )
    args = parser.parse_args()
    verbose = args.verbose

    # The WhatsApp session file holds Oscar's login keys: keep new files private.
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("whatsmeow", "neonize", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if verbose:
        logging.getLogger("oscar.whatsapp").setLevel(logging.DEBUG)
    # Oscar never sends media, so the missing-ffmpeg warning does not apply, and
    # the socket-close warning on Ctrl+C is expected.
    logging.getLogger("neonize.utils.ffmpeg").setLevel(logging.ERROR)
    logging.getLogger("whatsmeow.Client.Socket").setLevel(logging.ERROR)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        from app.whatsapp.bridge import WhatsAppBridge, unlink
    except ImportError as exc:
        if "libmagic" in str(exc):
            hint = "The libmagic system library is missing. Install it with: brew install libmagic"
        else:
            hint = (
                f"The WhatsApp library is not installed in this environment ({exc}).\n"
                "Run this with .venv-whatsapp/bin/python (see the README)."
            )
        print(hint, file=sys.stderr)
        sys.exit(1)

    if not cfg.openrouter_api_key:
        print("Note: OPENROUTER_API_KEY is not set in .env, so Oscar will apologise instead of answering.")

    store = Store(cfg.db_path)
    if args.unlink:
        try:
            unlinked, message = asyncio.run(unlink(cfg, store))
        finally:
            store.close()
        print(message, file=sys.stdout if unlinked else sys.stderr)
        sys.exit(0 if unlinked else 1)

    bridge = WhatsAppBridge(cfg, store)
    print("Oscar's WhatsApp connection is starting. Press Ctrl+C to stop.")
    try:
        asyncio.run(_run(bridge))
        state = store.get_whatsapp_state()
    finally:
        store.close()
    if state["last_status"] in ("logged_out", "error"):
        # Say why it stopped, here as well as on the dashboard.
        print(f"Stopped: {state['detail']}", file=sys.stderr)
    if bridge.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
