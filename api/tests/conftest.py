"""Test configuration.

Integration tests (tests/integration) need the central database image running
(see `make test-integration`) and TEST_DATABASE_URL pointing at it with the
owner role. Without it they are skipped, so `pytest` works anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

INTEGRATION_DIR = Path(__file__).parent / "integration"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_db = bool(os.environ.get("TEST_DATABASE_URL"))
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set (run `make test-integration`)")
    for item in items:
        if INTEGRATION_DIR in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.integration)
            if not has_db:
                item.add_marker(skip)
