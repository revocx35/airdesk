import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "common"), str(ROOT / "spyswitch"), str(ROOT / "airdesk"), str(ROOT / "common" / "tests")]
