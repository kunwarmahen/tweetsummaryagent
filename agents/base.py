"""Agent base class and shared context (inquiro-style)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import Settings
from db.models import AppSettings
from state import DigestRun


@dataclass
class AgentContext:
    """Domain-neutral object passed to every agent."""
    config: Settings           # bootstrap secrets/paths
    app_settings: AppSettings  # runtime config from DB
    logger: logging.Logger


class Agent:
    """Base agent. Each stage reads and mutates the shared DigestRun state."""
    name: str = "agent"

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self.log = ctx.logger.getChild(self.name)

    def run(self, state: DigestRun) -> DigestRun:  # pragma: no cover - interface
        raise NotImplementedError
