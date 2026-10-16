import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Keep the real user's settings, AI packages and tokens out of the tests (read when wallycull is imported)
os.environ["WALLYCULL_DATA"] = tempfile.mkdtemp(prefix="wallycull-test-")


import shutil

import pytest


@pytest.fixture(autouse=True)
def _fresh_shared_training():
    """Every test starts without ratings from other folders."""
    from wallycull import learning

    shutil.rmtree(learning.pool_dir(), ignore_errors=True)
    yield
