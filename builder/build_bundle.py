#!/usr/bin/env python3
"""Entry point: python builder/build_bundle.py -r requirements/packages.txt -o build/bundle"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mirror_builder.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
