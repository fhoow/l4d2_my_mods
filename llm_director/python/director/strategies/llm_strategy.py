"""Extensible LLM-backed AI Director strategy with Spec Section 5 guardrails and offline fallback."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from director.constants import (
    DIAGNOSIS_NAMES,
    DIRECTION_NAMES,
    HORDE_NAMES,
    INTENT_NAMES,
    ROLE_NAMES,
    BOSS_NAMES,
    BossDirective,
    HordeDirective,
    SituationDiagnosis,
    SpawnDirection,
    TacticalIntent,
    TargetRole,
    ZombieClass,
)
from director.guardrails import validate_and_sanitize_wave_plan
from director.models import FinaleBossDecision, TelemetrySnapshot, WavePlan
from director.strategies import BaseDirectorStrategy
from director.strategies.offline_state_machine import OfflineStateMachineStrategy

_CLASS_NAME_TO_ID = {
    "smoker": int(ZombieClass.SMOKER),
    "boomer": int(ZombieClass.BOOMER),
    "hunter": int(ZombieClass.HUNTER),
    "spitter": int(ZombieClass.SPITTER),
    "jockey": int(ZombieClass.JOCKEY),
    "charger": int(ZombieClass.CHARGER),
}


def _enum_by_name(value: Any, names: tuple[str, ...], default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        upper = value.strip().upper()
        for idx, name in enumerate(names):
            if name.upper() == upper:
                return idx
    return default


class LLMDirectorStrategy(BaseDirectorStrategy):
    """Calls an OpenAI-compatible / Ollama / local LLM endpoint for tactical wave planning.

    If no LLM endpoint is configured, or if the LLM request fails/times out, seamlessly
    delegates to `OfflineStateMachineStrategy` so gameplay never stalls.
    """

    name = "llm_hybrid"
    description = "LLM tactical director with Spec v1.2.0 guardrails and deterministic fallback"

    def __init__(self, timeout_sec: float = 2.0) -> None:
        self._fallback = OfflineStateMachineStrategy()
        self._timeout_sec = timeout_sec
        self._config_path = Path(__file__).resolve().parents[2] / "director_config.json"

    def _load_llm_config(self) -> tuple[str, str, str, float]:
        api_url = os.environ.get("L4D2_LLM_API_URL", "").strip()
        api_key = os.environ.get("L4D2_LLM_API_KEY", "").strip()
        model = os.environ.get("L4D2_LLM_MODEL", "gpt-4o-mini").strip()
        timeout = self._timeout_sec

        if not api_url and self._config_path.is_file():
            try:
                cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
                llm_cfg = cfg.get("llm", {})
                api_url = str(llm_cfg.get("api_url", "")).strip()
                api_key = str(llm_cfg.get("api_key", api_key)).strip()
                model = str(llm_cfg.get("model", model)).strip()
                timeout = float(llm_cfg.get("timeout_sec", timeout))
            except Exception:
                pass

        return api_url, api_key, model, timeout

    def _build_prompt(self, s: TelemetrySnapshot) -> str:
        summary = {
            "map": s.map_name,
            "wave_number": s.wave_number,
            "alive_count": s.alive_count,
            "down_or_dead_count": s.down_or_dead_count,
            "black_and_white_count": s.black_and_white_count,
            "avg_eff_hp": round(s.avg_eff_hp, 1),
            "team_ammo_avg_pct": round(s.team_ammo_avg_pct, 1),
            "medkits_total": s.medkits_total,
            "throwables_total": s.throwables_total,
            "front_gap_dist": round(s.front_gap_dist, 1),
            "rear_gap_dist": round(s.rear_gap_dist, 1),
            "max_spread_dist": round(s.max_spread_dist, 1),
            "peak_flow_ratio": round(s.peak_flow_ratio, 1),
            "flow_velocity": round(s.flow_velocity, 1),
            "stagnation_seconds": round(s.stagnation_seconds, 1),
            "last_wave_clear_duration": round(s.last_wave_clear_duration, 1),
            "wipe_count": s.wipe_count,
            "mvp_score_ratio": round(s.mvp_score_ratio, 2),
            "mvp_damage_share": round(s.mvp_damage_share, 2),
            "tank_alive": s.tank_alive,
            "finale_active": s.finale_active,
            "panic_event_active": s.panic_event_active,
            "last_wave_intent": INTENT_NAMES[s.last_wave_intent] if 0 <= s.last_wave_intent < len(INTENT_NAMES) else "NONE",
            "max_wave_si": s.max_wave_si,
        }
        return (
            "You are the L4D2 AI Director. Given the JSON telemetry below, return ONLY a compact JSON object "
            "with keys: diagnosis, intent, target_role, spawn_dir, horde_directive, boss_directive, "
            "freeze_ambush, focus_strike, cooldown, wave_target_count, si_classes, reason.\n"
            f"Valid diagnosis: {list(DIAGNOSIS_NAMES)}\n"
            f"Valid intent: {list(INTENT_NAMES)}\n"
            f"Valid target_role: {list(ROLE_NAMES)}\n"
            f"Valid spawn_dir: {list(DIRECTION_NAMES)}\n"
            f"Valid horde_directive: {list(HORDE_NAMES)}\n"
            f"Valid boss_directive: {list(BOSS_NAMES)}\n"
            "Valid si_classes: list of strings from ['Smoker','Boomer','Hunter','Spitter','Jockey','Charger']\n"
            f"Telemetry: {json.dumps(summary, ensure_ascii=False)}"
        )

    def _call_llm(self, api_url: str, api_key: str, model: str, timeout: float, prompt: str) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body["choices"][0]["message"]["content"]
        if isinstance(content, dict):
            return content
        return json.loads(content)

    def evaluate_finale_boss_stage(
        self,
        snapshot: TelemetrySnapshot,
        finale_type: int,
        arg: str = "",
    ) -> FinaleBossDecision:
        decision = self._fallback.evaluate_finale_boss_stage(snapshot, finale_type, arg)
        decision.strategy = self.name
        return decision

    def evaluate_wave(self, snapshot: TelemetrySnapshot) -> WavePlan:
        baseline = self._fallback.evaluate_wave(snapshot)
        if snapshot.alive_count == 0 or baseline.skip_wave:
            baseline.strategy = self.name
            return baseline

        api_url, api_key, model, timeout = self._load_llm_config()
        if not api_url:
            baseline.strategy = f"{self.name}(fallback_offline_sm)"
            return baseline

        try:
            raw = self._call_llm(api_url, api_key, model, timeout, self._build_prompt(snapshot))
            raw_classes = raw.get("si_classes", [])
            parsed_queue: list[int] = []
            if isinstance(raw_classes, list):
                for item in raw_classes:
                    if isinstance(item, int) and 1 <= item <= 6:
                        parsed_queue.append(item)
                    elif isinstance(item, str):
                        cid = _CLASS_NAME_TO_ID.get(item.strip().lower())
                        if cid:
                            parsed_queue.append(cid)

            plan = WavePlan(
                eval_seq=snapshot.eval_seq,
                strategy=self.name,
                skip_wave=False,
                diagnosis=_enum_by_name(raw.get("diagnosis"), DIAGNOSIS_NAMES, baseline.diagnosis),
                intent=_enum_by_name(raw.get("intent"), INTENT_NAMES, baseline.intent),
                target_role=_enum_by_name(raw.get("target_role"), ROLE_NAMES, baseline.target_role),
                target_survivor=0,
                spawn_dir=_enum_by_name(raw.get("spawn_dir"), DIRECTION_NAMES, baseline.spawn_dir),
                horde_directive=_enum_by_name(raw.get("horde_directive"), HORDE_NAMES, baseline.horde_directive),
                boss_directive=_enum_by_name(raw.get("boss_directive"), BOSS_NAMES, baseline.boss_directive),
                freeze_ambush=bool(raw.get("freeze_ambush", baseline.freeze_ambush)),
                focus_strike=bool(raw.get("focus_strike", baseline.focus_strike)),
                cooldown=int(raw.get("cooldown", baseline.cooldown)),
                wave_target_count=int(raw.get("wave_target_count", baseline.wave_target_count)),
                wave_queue=parsed_queue,
                clear_panic_flag=baseline.clear_panic_flag,
                reason=str(raw.get("reason", "llm_decision")),
            )
            return validate_and_sanitize_wave_plan(plan, snapshot)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
            baseline.strategy = f"{self.name}(fallback_err:{type(exc).__name__})"
            return baseline

    def get_status(self) -> dict[str, Any]:
        api_url, _, model, timeout = self._load_llm_config()
        return {
            "name": self.name,
            "description": self.description,
            "llm_configured": bool(api_url),
            "model": model,
            "timeout_sec": timeout,
        }
