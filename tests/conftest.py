import logging
from types import SimpleNamespace

import pytest

from agents.base import AgentContext


@pytest.fixture
def make_ctx():
    """Build an AgentContext with arbitrary app_settings attributes (no DB)."""
    def _make(**app_settings):
        return AgentContext(
            config=SimpleNamespace(),
            app_settings=SimpleNamespace(**app_settings),
            logger=logging.getLogger("test"),
        )
    return _make
