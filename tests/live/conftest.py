import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("QEVAL_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set QEVAL_LIVE=1 (calls the real Q API and/or Anthropic)")
    for item in items:
        item.add_marker(skip)
