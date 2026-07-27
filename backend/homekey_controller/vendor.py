from __future__ import annotations

import sys
from pathlib import Path


def activate_vendor() -> Path:
    """Expose the vendored upstream core's historical absolute imports."""
    vendor_root = (
        Path(__file__).resolve().parents[1]
        / "vendor"
        / "apple_home_key_reader"
    )
    value = str(vendor_root)
    if value not in sys.path:
        sys.path.insert(0, value)
    return vendor_root
