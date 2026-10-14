import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Keep the real user's settings, AI packages and tokens out of the tests (read when wallycull is imported)
os.environ["WALLYCULL_DATA"] = tempfile.mkdtemp(prefix="wallycull-test-")
