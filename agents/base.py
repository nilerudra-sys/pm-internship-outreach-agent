"""Common base class for pipeline agents."""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from config.settings import Settings
from database import repository


@dataclass
class AgentResult:
    """Outcome of one agent run, shown in the dashboard and activity log."""

    agent: str
    processed: int = 0
    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class BaseAgent(ABC):
    """An agent reads from and writes to the database; it never sends email unless approved."""

    name: str = "agent"

    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.log = logging.getLogger(f"agents.{self.name}")

    @abstractmethod
    def run(self, **kwargs) -> AgentResult:
        """Perform one unit of work and return a summary."""

    def record(self, action: str, details: str = "", entity_type: str = "agent", entity_id: int | None = None) -> None:
        repository.log_activity(self.conn, entity_type, entity_id, f"{self.name}:{action}", details)
