"""Centralized logger for the webapp.

Deliberately tiny: we only want logs for the things that, when they go wrong,
we need to be able to reconstruct what happened. That means:
  • Credit ledger mutations (topup / charge / refund / insufficient)
  • Auth decisions (SIWE/SIWS verify success + failure)
  • History imports (start / finish / failure + refund)
  • Treasury watcher actions (credited / unclaimed / skipped)
  • Any uncaught 500 from a route

Everything else (scan success, dashboard renders, routine API hits) stays
out of the log file to keep signal high. Rotating file handler caps the
log at 10 files × 2MB = 20MB on disk.

Usage:
    from applog import log
    log.info("credit.topup user=%s amount=%s tx=%s", user_id, amt, tx_hash)
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "webapp.log"


def _build_logger() -> logging.Logger:
    lg = logging.getLogger("portfolio")
    if lg.handlers:
        return lg  # already configured (uvicorn reload / test re-import)
    lg.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler: 2MB × 10 files = ~20MB capped disk use. Small
    # because we only log critical events, not request traces.
    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=10
    )
    fh.setFormatter(fmt)
    lg.addHandler(fh)

    # Also echo to stdout so `journalctl` / docker logs / tail -f work.
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    lg.propagate = False
    return lg


log = _build_logger()
