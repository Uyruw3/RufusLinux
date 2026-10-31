#!/usr/bin/env python3
"""Small wrapper so pkexec can run the engine without relying on PYTHONPATH."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rufus_linux.engine import main

if __name__ == "__main__":
    sys.exit(main())
