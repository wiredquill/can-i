import sys
from pathlib import Path

# Make src/ importable without requiring an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
