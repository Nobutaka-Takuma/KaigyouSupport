"""Vercel entry point for the API.

Vercel detects a FastAPI application by *reading* this file, not by running
it: it looks for a module-level name ``app`` in one of a fixed set of
locations, ``api/index.py`` among them. That detection is static, so the
import below must stay exactly where it is.

    DO NOT wrap this import in try/except, a function, or an ``if``.

Doing so hides the name from the detector and the build fails before anything
runs, with "Found api/index.py but it does not define a top-level 'app'
FastAPI instance" -- which reads like a problem with the application and is
not one. (Asked for a boot-time error message, this file once did exactly
that, and cost a deploy to learn it. Diagnosis lives in /api/health and in
the platform's runtime logs instead.)

The ETL does not run here. It is a laptop job that writes to the database
directly; this function only reads.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The working directory is not guaranteed, and config/ is otherwise found by
# walking up from it. Pointing at the bundle root removes the guesswork.
os.environ.setdefault("KAIGYOU_ROOT", str(ROOT))

# Normally the package arrives via ``./server`` in requirements.txt. This covers
# a bundle where the install did not happen but the sources are present.
_server = ROOT / "server"
if _server.is_dir() and str(_server) not in sys.path:
    sys.path.insert(0, str(_server))

from kaigyou_api.main import app  # noqa: E402  <- top level on purpose; see above

__all__ = ["app"]
