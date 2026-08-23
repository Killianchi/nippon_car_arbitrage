import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nippon_margin.config import load_config  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    """The real config.yaml -- these tests guard the shipped assumptions."""
    return load_config(ROOT / "config.yaml")
