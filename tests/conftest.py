"""Make `from skill.scripts import ...` resolve when running pytest from the repo root."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skill"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))
