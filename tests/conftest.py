from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
# Editable installs may put src after the root launcher with the same name.
if _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)
