"""Initialise (or reset) a file database with the demo scenario.

Usage:
    python -m app.init_db [path-to.db]
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import db as dbmod
from .seed import seed_demo


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "change.db"
    if path.exists():
        path.unlink()
    conn = dbmod.connect(path)
    dbmod.init_db(conn)
    info = seed_demo(conn)
    print(f"seeded demo graph into {path} (assessment as-of {info['assess_at']})")


if __name__ == "__main__":
    main()
