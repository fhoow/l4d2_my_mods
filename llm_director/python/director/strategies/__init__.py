"""Strategy base class and runtime registry with hot-reload support."""

from __future__ import annotations

import importlib
import sys
from abc import ABC, abstractmethod
from typing import Any

from director.models import FinaleBossDecision, TelemetrySnapshot, WavePlan


class BaseDirectorStrategy(ABC):
    """Interface that every AI Director strategy (offline SM, LLM, RL, etc.) implements."""

    name: str = "base"
    description: str = ""

    @abstractmethod
    def evaluate_wave(self, snapshot: TelemetrySnapshot) -> WavePlan:
        """Compute the next wave plan from the current telemetry snapshot."""

    @abstractmethod
    def evaluate_finale_boss_stage(
        self,
        snapshot: TelemetrySnapshot,
        finale_type: int,
        arg: str = "",
    ) -> FinaleBossDecision:
        """Compute BossDirective when L4D2 transitions into a Finale Boss stage."""

    def on_event(self, event_name: str, payload: dict[str, Any]) -> None:
        """Optional hook called on game events (round_start, wave_cleared, etc.)."""

    def on_tick(self, snapshot: TelemetrySnapshot) -> None:
        """Optional hook called on periodic heartbeat ticks from SourceMod."""

    def get_status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
        }


class StrategyRegistry:
    """Manages available strategies, active selection, and hot-reloading."""

    def __init__(self, default_strategy: str = "offline_sm") -> None:
        self._strategies: dict[str, BaseDirectorStrategy] = {}
        self._active_name: str = default_strategy
        self._load_builtin_strategies(reload_modules=False)

    def _load_builtin_strategies(self, reload_modules: bool) -> None:
        module_names = (
            "director.strategies.offline_state_machine",
            "director.strategies.llm_strategy",
            "director.strategies.gemini_decision_strategy",
        )
        for mod_name in module_names:
            if reload_modules and mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
            else:
                importlib.import_module(mod_name)

        from director.strategies.gemini_decision_strategy import GeminiDecisionStrategy
        from director.strategies.llm_strategy import LLMDirectorStrategy
        from director.strategies.offline_state_machine import OfflineStateMachineStrategy

        self._strategies = {}
        self.register(OfflineStateMachineStrategy())
        self.register(LLMDirectorStrategy())
        self.register(GeminiDecisionStrategy())

        if self._active_name not in self._strategies:
            self._active_name = "offline_sm"

    def register(self, strategy: BaseDirectorStrategy) -> None:
        self._strategies[strategy.name] = strategy

    @property
    def active_name(self) -> str:
        return self._active_name

    @property
    def active(self) -> BaseDirectorStrategy:
        return self._strategies[self._active_name]

    def list_names(self) -> list[str]:
        return sorted(self._strategies.keys())

    def set_active(self, name: str) -> BaseDirectorStrategy:
        key = name.strip()
        if key not in self._strategies:
            raise KeyError(f"Unknown strategy '{key}'. Available: {', '.join(self.list_names())}")
        self._active_name = key
        return self._strategies[key]

    def reload(self) -> list[str]:
        self._load_builtin_strategies(reload_modules=True)
        return self.list_names()
