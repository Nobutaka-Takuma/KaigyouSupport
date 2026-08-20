"""Vercel entry point for the API.

Vercel routes every ``/api/*`` request into this one function (see
``vercel.json``) and serves the built web client from the same domain, so the
browser never makes a cross-origin request and there is no CORS to configure.

The ETL does not run here. It is a laptop job that writes to the database
directly; this function only reads.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The function's working directory is not guaranteed, and config/ is found by
# walking up from it. Pointing at the bundle root removes the guesswork.
os.environ.setdefault("KAIGYOU_ROOT", str(ROOT))

# Present when the package is installed from requirements.txt; this covers a
# bundle where it is not.
_server = ROOT / "server"
if _server.is_dir() and str(_server) not in sys.path:
    sys.path.insert(0, str(_server))

from kaigyou_api.main import app  # noqa: E402

__all__ = ["app"]
