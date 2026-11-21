#!/usr/bin/env python3
"""pnc-bot entry point (run with ``python3 -I bot.py``)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pnc_bot import app  # noqa: E402

if __name__ == "__main__":
    sys.exit(app.main())
