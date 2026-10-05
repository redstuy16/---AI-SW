"""지속되는 재개 정보와 명시적 충돌 주입 경계."""
from __future__ import annotations

from typing import Callable

from pydantic import Field

from .schemas import StrictModel


class ResearchRuntimeCursor(StrictModel):
    research_id: str
    stage: str
    action_index: int = Field(ge=0)
    active_hypotheses: list[str] = Field(default_factory=list)
    active_tasks: list[str] = Field(default_factory=list)
    active_contracts: list[str] = Field(default_factory=list)
    active_experiments: list[str] = Field(default_factory=list)
    pending_actions: list[str] = Field(default_factory=list)
    retry_counters: dict[str, int] = Field(default_factory=dict)
    branch_depths: dict[str, int] = Field(default_factory=dict)
    loop_fingerprints: dict[str, int] = Field(default_factory=dict)
    state_version: int = Field(ge=0)
    last_event_seq: int = Field(ge=0)
    last_planning_seq: int = Field(ge=0)


class InjectedCrash(BaseException):
    """일반 예외 복구 없이 프로세스 종료를 모사한다."""


class FaultInjector:
    def __init__(self, trigger: Callable[[str], None] | None = None):
        self._trigger = trigger

    def at(self, boundary: str) -> None:
        if self._trigger is not None:
            self._trigger(boundary)
