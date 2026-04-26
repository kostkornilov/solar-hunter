from __future__ import annotations

import sys
from pathlib import Path


def pytest_configure() -> None:
    """Ensure `src/...` imports resolve to dem_api's internal package.

    dem_api keeps its implementation under `dem_api/src/` (a Python package named `src`).
    When running tests from the repo root, there is also a top-level `src/` folder, so we
    force Python to prefer dem_api's package by putting the dem_api root first on sys.path.
    """

    dem_api_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(dem_api_root))
