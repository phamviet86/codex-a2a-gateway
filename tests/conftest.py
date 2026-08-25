from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_a2a import FakeA2AServer  # noqa: E402


@pytest.fixture
def fake_a2a() -> FakeA2AServer:
    server = FakeA2AServer().start()
    try:
        yield server
    finally:
        server.close()
