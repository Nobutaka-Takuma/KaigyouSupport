"""``python -m kaigyou_etl`` -- the same command line as ``kaigyou-etl``.

The console script is the documented way in, but it only exists on PATH once
the package is installed, and on Windows it needs a path prefix
(``.\\.venv\\Scripts\\kaigyou-etl``) that is easy to forget. ``python -m`` is
what people reach for instead, and without this file it answers with
"'kaigyou_etl' is a package and cannot be directly executed" -- which is true
and unhelpful, since the package plainly does have a command line.
"""
from __future__ import annotations

import sys

from kaigyou_etl.cli import main

if __name__ == "__main__":
    sys.exit(main())
