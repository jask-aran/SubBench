from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_meter_state():
    # The Claude meter keeps a rate-limit backoff and a cached profile for the life of the
    # process. Each test starts from none, so one test's 429 cannot defer another's read.
    from subbench import endpoints, entitlement

    endpoints._PROFILE.clear()
    entitlement._CLAUDE_METER.clear()
    yield
