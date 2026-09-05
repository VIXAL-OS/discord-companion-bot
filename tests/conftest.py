"""Test fixtures: repo root on sys.path so `import bot` works wherever pytest
runs from. No Discord, no Anthropic, no network — the pins drive the real
method bodies with duck-typed collaborators."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
