"""Run the console locally: python -m sentinel.console [--host H] [--port P] [--reload]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from ..config import settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sentinel.console", description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", settings.console_port)))
    ap.add_argument("--poll", type=float, default=2.0, help="Firestore poll interval in seconds.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .app import create_app  # noqa: PLC0415

    app = create_app(poll_interval_s=args.poll)
    print(f"Sentinel Console on http://localhost:{args.port}  (farm {settings.demo_farm_id}, poll {args.poll}s)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
