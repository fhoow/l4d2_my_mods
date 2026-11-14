"""Gemini Decision 1 (`gemini-decision-1`) AI Director strategy.

Uses the Google GenAI Decisions API (`client.decisions.create` on `v1beta`) to evaluate
structured `choice`, `noul` (yes/no), and `score` (rubric) questions in a single
low-latency forward pass (zero output tokens), then enforces Spec v1.2.0 guardrails
with seamless fallback to `OfflineStateMachineStrategy`.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time
from typing import Any

from dotenv import load_dotenv

from director.constants import (
    BOSS_NAMES,
    DIAGNOSIS_NAMES,
    DIRECTION_NAMES,
    HORDE_NAMES,
    INTENT_NAMES,
    ROLE_NAMES,
    BossDirective,
    HordeDirective,
    SituationDiagnosis,
    SpawnDirection,
    TacticalIntent,
    TargetRole,
    ZombieClass,
)
from director.guardrails import (
    build_wave_composition,
    clamp_int,
    compute_target_si_limit,
    enforce_anti_hard_cc_cap,
    validate_and_sanitize_wave_plan,
)
from director.models import FinaleBossDecision, TelemetrySnapshot, WavePlan
from director.strategies import BaseDirectorStrategy
from director.strategies.offline_state_machine import OfflineStateMachineStrategy

_LOGGER = logging.getLogger("l4d2_director_rpc")
_AUDIT_LOGGER_NAME = "l4d2_gemini_decision_audit"

_PYTHON_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_AUDIT_LOG_PATH = _PYTHON_DIR / "logs" / "gemini_decision_audit.log"
_ENV_CANDIDATES = (_PYTHON_DIR / ".env", _PYTHON_DIR.parent / ".env")

_CLASS_NAME_TO_ID = {
    "smoker": int(ZombieClass.SMOKER),
    "boomer": int(ZombieClass.BOOMER),
    "hunter": int(ZombieClass.HUNTER),
    "spitter": int(ZombieClass.SPITTER),
    "jockey": int(ZombieClass.JOCKEY),
    "charger": int(ZombieClass.CHARGER),
}

_PERSONA_PREAMBLE = (
    "DIRECTOR PERSONA: STRICT, RUTHLESS, AND UNFORGIVING. "
    "Your goal is to make survival extremely difficult and punish every tactical flaw "
    "(isolation, camping, slow pace, over-confidence, high HP/ammo, or relying on one MVP). "
    "Only show mercy if the team is genuinely on the brink of a total wipe."
)

_ENV_LOADED = False


def _load_env_files() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    for env_path in _ENV_CANDIDATES:
        if env_path.is_file():
            load_dotenv(env_path, override=False)
    _ENV_LOADED = True


def _enum_by_name(value: Any, names: tuple[str, ...], default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        upper = value.strip().upper()
        for idx, name in enumerate(names):
            if name.upper() == upper:
                return idx
    return default


def _ans_field(answer: Any, key: str, default: Any = None) -> Any:
    if answer is None:
        return default
    if isinstance(answer, dict):
        return answer.get(key, default)
    return getattr(answer, key, default)


def _compose_wave_queue_from_decisions(
    answers: dict[str, Any],
    intent: int,
    wave_target_count: int,
    alive_count: int,
    active_si_counts: dict[int, int],
) -> list[int]:
    """Build wave_queue using lead/support choices + D'Hondt ranked-quota from probabilities."""
    wave_queue: list[int] = []
    for q_key in ("lead_si_class", "support_si_class"):
        cls_name = str(_ans_field(answers.get(q_key), "choice", "") or "").strip().lower()
        zc = _CLASS_NAME_TO_ID.get(cls_name)
        if zc and len(wave_queue) < wave_target_count:
            wave_queue.append(
                enforce_anti_hard_cc_cap(zc, alive_count, active_si_counts, wave_queue)
            )

    if len(wave_queue) >= wave_target_count:
        return wave_queue

    # Combine calibrated probability distributions (lead 0.6 + support 0.4)
    class_scores: dict[int, float] = {}
    for q_key, weight in (("lead_si_class", 0.6), ("support_si_class", 0.4)):
        probs = _ans_field(answers.get(q_key), "probabilities", None)
        if isinstance(probs, dict):
            for raw_name, raw_p in probs.items():
                zc = _CLASS_NAME_TO_ID.get(str(raw_name).strip().lower())
                if zc:
                    class_scores[zc] = class_scores.get(zc, 0.0) + weight * max(0.0, float(raw_p))

    if sum(class_scores.values()) > 0.0:
        while len(wave_queue) < wave_target_count:
            ranked = sorted(
                class_scores.keys(),
                key=lambda zc: (
                    class_scores[zc] / (1 + sum(1 for item in wave_queue if item == zc)),
                    class_scores[zc],
                ),
                reverse=True,
            )
            chosen: int | None = None
            for cand in ranked:
                if enforce_anti_hard_cc_cap(cand, alive_count, active_si_counts, wave_queue) == cand:
                    chosen = cand
                    break
            if chosen is None:
                chosen = enforce_anti_hard_cc_cap(ranked[0], alive_count, active_si_counts, wave_queue)
            wave_queue.append(chosen)
        return wave_queue

    remainder = build_wave_composition(
        intent,
        wave_target_count - len(wave_queue),
        alive_count,
        active_si_counts,
    )
    for zc in remainder:
        wave_queue.append(enforce_anti_hard_cc_cap(zc, alive_count, active_si_counts, wave_queue))
    return wave_queue


def _build_wave_questions() -> dict[str, dict[str, Any]]:
    """Build closed-set questions for `gemini-decision-1` EvaluateWave."""
    return {
        "diagnosis": {
            "type": "choice",
            "instructions": (
                "Diagnose the survivors' current tactical vulnerability so a strict Director "
                "can exploit it ruthlessly."
            ),
            "criteria": {
                "HEALTHY_STEADY_PUSH": "Team is healthy, grouped, and advancing steadily.",
                "SPEEDRUNNER_ISOLATED": "Front player has pulled far ahead (large front_gap_dist) and is exposed.",
                "REAR_STRAGGLER_EXPOSED": "Rear player is lagging far behind (large rear_gap_dist) and isolated.",
                "MVP_PLAYER_CARRYING": "One MVP player dominates kills/damage (high mvp_score_ratio/share) and carries the team.",
                "CORNER_CAMPING_STRONG": "Team is stationary/stagnating with strong HP/ammo, holding a defensive corner.",
                "TEAM_CRITICAL_EXHAUSTED": "Multiple survivors are down/dead or black-and-white with critical HP and low ammo.",
                "NATIVE_INFINITE_RUSH": "A panic event or endless crescendo horde is currently active.",
                "FINALE_BOSS_STAGE": "A finale boss stage is active.",
            },
        },
        "intent": {
            "type": "choice",
            "instructions": (
                "Select the ruthless Director tactical intent. Prioritize maximum pressure, "
                "punishing mistakes, and draining survivor resources; avoid mercy unless wipe is imminent."
            ),
            "criteria": {
                "ESCALATE_CLIMAX": "Maximum lethal pressure: overwhelm healthy/fast survivors with heavy SI, horde, and Boss threat.",
                "SNIPE_LONE_WOLF": "Execute an isolated rusher or straggler immediately with hard-pinning Special Infected.",
                "ASSASSINATE_MVP": "Focus-fire and neutralize the carrying MVP to collapse the team's backbone.",
                "BREAK_CAMPING_FORMATION": "Force campers/stagnating survivors out of cover using area-denial and disruption.",
                "MAINTAIN_GAUNTLET_FLOW": "Apply relentless attrition pressure during normal movement.",
                "MERCY_RELAX_ALLOW_HEAL": "LAST RESORT ONLY: briefly ease pressure when 2+ survivors are down/dead or critically exhausted.",
            },
        },
        "target_role": {
            "type": "choice",
            "instructions": "Choose which survivor role should be the primary target of this wave's ambush.",
            "criteria": {
                "P1_FRONT": "Target the lead rusher at the front to stop momentum.",
                "P4_REAR": "Target the trailing straggler at the rear to force a costly rescue.",
                "P_MVP": "Target the highest-impact MVP carrying the team.",
                "P_VULNERABLE": "Target the lowest-HP, black-and-white, or most fragile survivor to secure a kill.",
                "NONE": "No specific target priority; distribute pressure across the team.",
            },
        },
        "spawn_dir": {
            "type": "choice",
            "instructions": "Choose the most punishing ambush spawn direction relative to the target.",
            "criteria": {
                "FRONT": "Block and ambush from ahead along their path.",
                "MIDDLE_FLANK": "Strike from the side flank to split the formation.",
                "BEHIND": "Ambush from behind to catch stragglers or pinch during a fight.",
                "ABOVE_ROOF": "Drop from high ground/rooftops for surprise Hunter/Jockey/Spitter strikes.",
                "BELOW_FLOOR": "Attack from lower elevation or blind spots below.",
            },
        },
        "horde_directive": {
            "type": "choice",
            "instructions": (
                "Choose common infected horde intensity. A strict Director favors heavy swarms "
                "(SCALE_1.6X or RIOT_SQUAD) whenever survivors are not near total wipe."
            ),
            "criteria": {
                "SCALE_1.6X": "Heavy aggressive horde (1.25x) to overwhelm and drain ammo/HP.",
                "RIOT_SQUAD": "Spawn armored riot/uncommon squad to break camping or stall pushes.",
                "SCALE_1.2X": "Standard elevated horde pressure (1.0x).",
                "KEEP_NATIVE": "Keep native L4D2 horde pacing.",
                "SCALE_0.5X": "Reduced horde (0.5x), ONLY when granting last-resort mercy.",
            },
        },
        "boss_directive": {
            "type": "choice",
            "instructions": (
                "Decide Tank pressure for this wave. Note: non-boss finale stages forbid extra Tanks, "
                "and if a Tank is already alive do not stack unnecessarily unless team is dominating."
            ),
            "criteria": {
                "RESPECT_NATIVE": "Let native Tank rules apply without extra Tanks.",
                "ADD_1_TANK": "Inject 1 extra Tank to severely test healthy or fast-clearing survivors.",
                "ADD_2_TANKS": "Inject 2 Tanks to crush an overly healthy, well-armed, zero-wipe team.",
                "BLOCK_EXTRA_RESPAWN": "Suppress extra Tanks only when team is critically battered or multiple wipes occurred.",
            },
        },
        "use_freeze_ambush": {
            "type": "noul",
            "instructions": (
                "Should the Director stage a synchronized freeze-ambush so Special Infected attack "
                "together instead of trickling in one by one?"
            ),
            "criteria": {
                "true_description": "Hold and coordinate SI for a lethal simultaneous strike (preferred for strict pressure).",
                "false_description": "Allow immediate staggered spawns (e.g., during active panic/rush or pure attrition).",
            },
        },
        "use_focus_strike": {
            "type": "noul",
            "instructions": (
                "Should all Special Infected lock onto the single primary target survivor to guarantee a down/kill?"
            ),
            "criteria": {
                "true_description": "Focus-fire one vulnerable, isolated, or MVP target ruthlessly.",
                "false_description": "Spread Special Infected across multiple survivors.",
            },
        },
        "lead_si_class": {
            "type": "choice",
            "instructions": "Select the lead Special Infected class to initiate the ambush.",
            "criteria": {
                "Charger": "Shatter tight formations/campers or slam isolated targets.",
                "Hunter": "High-DPS pounce to punish lone wolves or distracted survivors.",
                "Smoker": "Pull stragglers, roof-snipe, or drag survivors off ledges/out of cover.",
                "Jockey": "Drag targets away from teammates into hazards or deeper zombie packs.",
                "Spitter": "Force campers out of corners and melt pinned/downed survivors.",
                "Boomer": "Blind the team and trigger chaotic common-infected pileups.",
            },
        },
        "support_si_class": {
            "type": "choice",
            "instructions": "Select the best secondary Special Infected class to combo with the lead class.",
            "criteria": {
                "Spitter": "Acid combo on pinned, downed, or cornered survivors.",
                "Boomer": "Bile-blind responders trying to rescue a pinned teammate.",
                "Smoker": "Secondary pull to disrupt rescuers.",
                "Hunter": "Follow-up pounce damage during chaos.",
                "Jockey": "Steer rescuers away from pinned teammates.",
                "Charger": "Bowling disruption against grouped rescuers.",
            },
        },
        "aggression_score": {
            "type": "score",
            "instructions": (
                "Rate how aggressively the strict Director should punish the survivors this wave "
                "(determines shorter wave cooldown and higher Special Infected count)."
            ),
            "criteria": [
                "0 - Minimal pressure (rare emergency mercy only)",
                "1 - Light attrition (team heavily wounded but still pushing)",
                "2 - Firm pressure (standard strict gauntlet harassment)",
                "3 - Harsh punishment (short cooldown, heavy SI count to drain resources)",
                "4 - Ruthless wipe attempt (minimum cooldown, maximum SI cap, relentless combo)",
            ],
        },
    }


def _build_finale_questions() -> dict[str, dict[str, Any]]:
    return {
        "boss_directive": {
            "type": "choice",
            "instructions": (
                "A Finale Boss stage has started. As a strict, unforgiving Director, decide the Tank count. "
                "Prefer ADD_1_TANK or ADD_2_TANKS unless the survivors are genuinely on the verge of wiping."
            ),
            "criteria": {
                "ADD_2_TANKS": "Survivors are healthy, well-supplied, or have 0 wipes — punish them with double Tanks.",
                "ADD_1_TANK": "Survivors are moderately intact — add 1 extra Tank to keep finale lethal.",
                "RESPECT_NATIVE": "Survivors already suffered heavy downs/wipes or low HP — keep native Tank.",
                "BLOCK_EXTRA_RESPAWN": "Survivors are critically exhausted (2+ down/dead or <35 HP) — block extra respawn.",
            },
        }
    }


_WAVE_QUESTIONS = _build_wave_questions()
_FINALE_QUESTIONS = _build_finale_questions()


def _select_wave_questions(snapshot: TelemetrySnapshot) -> dict[str, dict[str, Any]]:
    """Omit questions whose answers are deterministically locked by guardrails/state."""
    if (
        snapshot.tank_alive
        or snapshot.is_native_horde_finale_stage
        or (snapshot.finale_active and not snapshot.is_native_boss_finale_stage)
    ):
        return {k: v for k, v in _WAVE_QUESTIONS.items() if k != "boss_directive"}
    return _WAVE_QUESTIONS


def _get_audit_logger() -> logging.Logger:
    logger = logging.getLogger(_AUDIT_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    _DEFAULT_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        _DEFAULT_AUDIT_LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return _to_jsonable(obj.model_dump(mode="json", exclude_none=True))
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {
            str(k): _to_jsonable(v)
            for k, v in vars(obj).items()
            if not str(k).startswith("_") and v is not None
        }
    return str(obj)


class GeminiDecisionStrategy(BaseDirectorStrategy):
    """Strict/ruthless AI Director powered by Google `gemini-decision-1` Decisions API."""

    name = "gemini_decision"
    description = (
        "Strict/punishing AI Director using Gemini Decisions API (gemini-decision-1) "
        "with Spec v1.2.0 guardrails and deterministic fallback"
    )

    def __init__(
        self,
        timeout_sec: float = 2.5,
        client: Any | None = None,
        audit_log_path: Path | str | None = None,
    ) -> None:
        self._fallback = OfflineStateMachineStrategy()
        self._default_timeout_sec = timeout_sec
        self._injected_client = client
        self._cached_client: Any | None = None
        self._cached_client_key: tuple[str, int] | None = None
        self._audit_log_path = Path(audit_log_path) if audit_log_path is not None else None
        self._audit_lock = threading.Lock()
        self._audit_call_count = 0
        self._audit_total_input_tokens = 0
        self._audit_total_tokens = 0

    def _load_config(self) -> tuple[str, str, float]:
        _load_env_files()
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        model = os.environ.get("GEMINI_DECISION_MODEL", "gemini-decision-1").strip() or "gemini-decision-1"
        raw_timeout = os.environ.get("GEMINI_DECISION_TIMEOUT_SEC", "").strip()
        try:
            timeout_sec = float(raw_timeout) if raw_timeout else self._default_timeout_sec
        except ValueError:
            timeout_sec = self._default_timeout_sec
        timeout_sec = max(0.5, min(10.0, timeout_sec))
        return api_key, model, timeout_sec

    def _get_client(self, api_key: str, timeout_sec: float) -> Any:
        if self._injected_client is not None:
            return self._injected_client

        timeout_ms = max(500, int(round(timeout_sec * 1000)))
        cache_key = (api_key, timeout_ms)
        if self._cached_client is not None and self._cached_client_key == cache_key:
            return self._cached_client

        from google import genai

        self._cached_client = genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta", "timeout": timeout_ms},
        )
        self._cached_client_key = cache_key
        return self._cached_client

    def _build_telemetry_input(self, s: TelemetrySnapshot, extra_context: str = "") -> str:
        last_intent = (
            INTENT_NAMES[s.last_wave_intent]
            if 0 <= s.last_wave_intent < len(INTENT_NAMES)
            else "NONE"
        )
        lines = [
            _PERSONA_PREAMBLE,
            f"Map: {s.map_name} | Wave: {s.wave_number + 1} | GameTime: {s.game_time:.1f}s",
            (
                f"Flags: finale_active={s.finale_active} (stage={s.cur_finale_stage}), "
                f"panic_event_active={s.panic_event_active}, tank_alive={s.tank_alive}, "
                f"last_wave_intent={last_intent}"
            ),
            (
                f"Team Health: alive={s.alive_count}, down_or_dead={s.down_or_dead_count}, "
                f"black_and_white={s.black_and_white_count}, avg_eff_hp={s.avg_eff_hp:.1f}, "
                f"wipe_count={s.wipe_count}"
            ),
            (
                f"Resources & Pace: team_ammo_avg_pct={s.team_ammo_avg_pct:.1f}%, "
                f"medkits_total={s.medkits_total}, throwables_total={s.throwables_total}, "
                f"last_wave_clear_duration={s.last_wave_clear_duration:.1f}s, "
                f"peak_flow_ratio={s.peak_flow_ratio:.1f}%, flow_velocity={s.flow_velocity:.1f}, "
                f"stagnation_seconds={s.stagnation_seconds:.1f}s"
            ),
            (
                f"Formation & MVP: front_gap_dist={s.front_gap_dist:.1f}, "
                f"rear_gap_dist={s.rear_gap_dist:.1f}, max_spread_dist={s.max_spread_dist:.1f}, "
                f"mvp_score_ratio={s.mvp_score_ratio:.2f}, mvp_damage_share={s.mvp_damage_share:.2f}, "
                f"mvp_kills_si={s.mvp_kills_si}"
            ),
            (
                f"Constraints: active_si_total={s.active_si_total}, max_wave_si={s.max_wave_si}, "
                f"cooldown_range=[{s.min_cooldown}, {s.max_cooldown}]"
            ),
        ]
        if extra_context:
            lines.append(extra_context)

        if s.survivors:
            surv_parts: list[str] = []
            for surv in s.survivors:
                roles: list[str] = []
                if surv.client == s.p1_front:
                    roles.append("P1_FRONT")
                if surv.client == s.p4_rear:
                    roles.append("P4_REAR")
                if surv.client == s.p_mvp:
                    roles.append("P_MVP")
                if surv.client == s.p_vulnerable:
                    roles.append("P_VULNERABLE")
                role_tag = ",".join(roles) if roles else "TEAMMATE"
                surv_parts.append(
                    f"- {surv.name or f'client_{surv.client}'} [{role_tag}]: "
                    f"alive={surv.alive}, incap={surv.incap_or_hanging}, bnw={surv.black_and_white}, "
                    f"pinned={surv.pinned}, eff_hp={surv.eff_hp:.0f}, ammo={surv.ammo_pct:.0f}%, "
                    f"medkit={surv.has_medkit}, throwable={surv.has_throwable}, kills_si={surv.kills_si}"
                )
            lines.append("Survivors:\n" + "\n".join(surv_parts))

        return "\n".join(lines)

    def on_event(self, event_name: str, payload: dict[str, Any]) -> None:
        self._fallback.on_event(event_name, payload)

    def on_tick(self, snapshot: TelemetrySnapshot) -> None:
        self._fallback.on_tick(snapshot)

    def _record_decision_audit(
        self,
        *,
        call_type: str,
        snapshot: TelemetrySnapshot,
        model: str,
        latency_ms: float,
        resp: Any,
    ) -> None:
        raw_usage = getattr(resp, "usage", None)
        if raw_usage is None:
            raw_usage = getattr(resp, "usage_metadata", None)
        usage_dict = _to_jsonable(raw_usage) if raw_usage is not None else {}
        if not isinstance(usage_dict, dict):
            usage_dict = {"raw": usage_dict}

        raw_answers = getattr(resp, "answers", None) or {}
        answers_dict = _to_jsonable(raw_answers)

        in_tokens = int(usage_dict.get("total_input_tokens", 0) or 0)
        tot_tokens = int(usage_dict.get("total_tokens", 0) or 0)

        with self._audit_lock:
            self._audit_call_count += 1
            self._audit_total_input_tokens += in_tokens
            self._audit_total_tokens += tot_tokens
            cum_calls = self._audit_call_count
            cum_in = self._audit_total_input_tokens
            cum_tot = self._audit_total_tokens

        entry: dict[str, Any] = {
            "call_type": call_type,
            "eval_seq": snapshot.eval_seq,
            "map_name": snapshot.map_name,
            "wave_number": snapshot.wave_number + (1 if call_type == "EvaluateWave" else 0),
            "model": model,
            "latency_ms": round(latency_ms, 1),
            "usage": usage_dict,
            "cumulative_usage": {
                "calls": cum_calls,
                "total_input_tokens": cum_in,
                "total_tokens": cum_tot,
            },
            "answers": answers_dict,
        }
        raw_feedback = getattr(resp, "prompt_feedback", None)
        if raw_feedback is not None:
            entry["prompt_feedback"] = _to_jsonable(raw_feedback)

        payload = json.dumps(entry, ensure_ascii=False)
        try:
            if self._audit_log_path is not None:
                self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                with self._audit_lock:
                    with self._audit_log_path.open("a", encoding="utf-8") as fp:
                        fp.write(f"[{ts}] {payload}\n")
            else:
                _get_audit_logger().info("%s", payload)
        except Exception as exc:
            _LOGGER.warning("Failed to write Gemini decision audit log: %s", exc)

    def evaluate_finale_boss_stage(
        self,
        snapshot: TelemetrySnapshot,
        finale_type: int,
        arg: str = "",
    ) -> FinaleBossDecision:
        baseline = self._fallback.evaluate_finale_boss_stage(snapshot, finale_type, arg)
        if snapshot.alive_count == 0 or baseline.boss_directive == int(BossDirective.BLOCK_EXTRA_RESPAWN):
            baseline.strategy = self.name
            baseline.reason = f"gemini_shortcircuit(critical_finale_stage={finale_type})"
            return baseline

        api_key, model, timeout_sec = self._load_config()
        if not api_key and self._injected_client is None:
            baseline.strategy = f"{self.name}(fallback_no_key)"
            return baseline

        try:
            client = self._get_client(api_key, timeout_sec)
            input_text = self._build_telemetry_input(
                snapshot,
                extra_context=f"FinaleBossStage: finale_type={finale_type}, arg={arg}",
            )
            t0 = time.perf_counter()
            resp = client.decisions.create(
                model=model,
                input=input_text,
                questions=_FINALE_QUESTIONS,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._record_decision_audit(
                call_type="EvaluateFinaleBossStage",
                snapshot=snapshot,
                model=model,
                latency_ms=latency_ms,
                resp=resp,
            )
            answers = getattr(resp, "answers", None) or {}
            boss_ans = answers.get("boss_directive")
            boss_choice = _ans_field(boss_ans, "choice")
            boss_conf = float(_ans_field(boss_ans, "confidence", 0.0) or 0.0)
            boss_dir = _enum_by_name(boss_choice, BOSS_NAMES, baseline.boss_directive)

            return FinaleBossDecision(
                eval_seq=snapshot.eval_seq,
                finale_type=int(finale_type),
                strategy=self.name,
                diagnosis=int(SituationDiagnosis.FINALE_BOSS_STAGE),
                boss_directive=int(boss_dir),
                alive_survivors=int(snapshot.alive_count),
                reason=f"gemini_decision_finale(stage={finale_type},conf={boss_conf:.2f})",
            )
        except Exception as exc:
            _LOGGER.warning("GeminiDecisionStrategy finale fallback (%s): %s", type(exc).__name__, exc)
            baseline.strategy = f"{self.name}(fallback_err:{type(exc).__name__})"
            return baseline

    def evaluate_wave(self, snapshot: TelemetrySnapshot) -> WavePlan:
        baseline = self._fallback.evaluate_wave(snapshot)
        if snapshot.alive_count == 0 or baseline.skip_wave:
            baseline.strategy = self.name
            return baseline

        if baseline.intent == int(TacticalIntent.MERCY_RELAX_ALLOW_HEAL):
            baseline.strategy = self.name
            baseline.reason = "gemini_shortcircuit(critical_mercy)"
            return baseline

        if (
            snapshot.stagnation_seconds >= 180.0
            and snapshot.wave_damage_taken == 0
            and snapshot.flow_velocity <= 0.5
        ):
            baseline.strategy = self.name
            baseline.reason = "gemini_shortcircuit(afk_stagnation)"
            return baseline

        api_key, model, timeout_sec = self._load_config()
        if not api_key and self._injected_client is None:
            baseline.strategy = f"{self.name}(fallback_no_key)"
            return baseline

        try:
            client = self._get_client(api_key, timeout_sec)
            t0 = time.perf_counter()
            resp = client.decisions.create(
                model=model,
                input=self._build_telemetry_input(snapshot),
                questions=_select_wave_questions(snapshot),
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._record_decision_audit(
                call_type="EvaluateWave",
                snapshot=snapshot,
                model=model,
                latency_ms=latency_ms,
                resp=resp,
            )
            answers = getattr(resp, "answers", None) or {}
            if not answers:
                raise ValueError("Empty answers from gemini-decision-1")

            diagnosis = _enum_by_name(
                _ans_field(answers.get("diagnosis"), "choice"),
                DIAGNOSIS_NAMES,
                baseline.diagnosis,
            )
            intent = _enum_by_name(
                _ans_field(answers.get("intent"), "choice"),
                INTENT_NAMES,
                baseline.intent,
            )
            target_role = _enum_by_name(
                _ans_field(answers.get("target_role"), "choice"),
                ROLE_NAMES,
                baseline.target_role,
            )
            spawn_dir = _enum_by_name(
                _ans_field(answers.get("spawn_dir"), "choice"),
                DIRECTION_NAMES,
                baseline.spawn_dir,
            )
            horde_directive = _enum_by_name(
                _ans_field(answers.get("horde_directive"), "choice"),
                HORDE_NAMES,
                baseline.horde_directive,
            )
            boss_directive = _enum_by_name(
                _ans_field(answers.get("boss_directive"), "choice"),
                BOSS_NAMES,
                baseline.boss_directive,
            )

            freeze_prob = float(_ans_field(answers.get("use_freeze_ambush"), "noul", 0.5) or 0.5)
            focus_prob = float(_ans_field(answers.get("use_focus_strike"), "noul", 0.5) or 0.5)
            freeze_ambush = freeze_prob >= 0.5
            focus_strike = focus_prob >= 0.5

            aggression = float(_ans_field(answers.get("aggression_score"), "score", 2.5) or 2.5)
            aggression = max(0.0, min(4.0, aggression))

            # Higher aggression -> shorter cooldown within [min_cooldown, max_cooldown]
            agg_ratio = aggression / 4.0
            cooldown_span = max(0, snapshot.max_cooldown - snapshot.min_cooldown)
            cooldown = int(round(snapshot.max_cooldown - cooldown_span * agg_ratio))
            cooldown = clamp_int(cooldown, snapshot.min_cooldown, snapshot.max_cooldown)

            target_si_limit = compute_target_si_limit(snapshot.alive_count, snapshot.max_wave_si)
            if intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL:
                wave_target_count = clamp_int(max(1, target_si_limit // 2), 1, target_si_limit)
            else:
                min_strict_count = max(1, target_si_limit - 1) if aggression >= 2.0 else max(1, target_si_limit - 2)
                scaled_count = int(round(min_strict_count + (target_si_limit - min_strict_count) * agg_ratio))
                wave_target_count = clamp_int(scaled_count, 1, target_si_limit)

            # Build wave queue starting with lead + support SI and ranked-quota from AI probabilities
            wave_queue = _compose_wave_queue_from_decisions(
                answers,
                intent,
                wave_target_count,
                snapshot.alive_count,
                snapshot.active_si_counts,
            )

            plan = WavePlan(
                eval_seq=snapshot.eval_seq,
                strategy=self.name,
                skip_wave=False,
                diagnosis=diagnosis,
                intent=intent,
                target_role=target_role,
                target_survivor=0,
                spawn_dir=spawn_dir,
                horde_directive=horde_directive,
                boss_directive=boss_directive,
                freeze_ambush=freeze_ambush,
                focus_strike=focus_strike,
                cooldown=cooldown,
                wave_target_count=wave_target_count,
                wave_queue=wave_queue,
                clear_panic_flag=baseline.clear_panic_flag,
                reason=f"gemini_decision(agg={aggression:.2f},freeze={freeze_prob:.2f},focus={focus_prob:.2f})",
            )
            return validate_and_sanitize_wave_plan(plan, snapshot)
        except Exception as exc:
            _LOGGER.warning("GeminiDecisionStrategy wave fallback (%s): %s", type(exc).__name__, exc)
            baseline.strategy = f"{self.name}(fallback_err:{type(exc).__name__})"
            return baseline

    def get_status(self) -> dict[str, Any]:
        api_key, model, timeout_sec = self._load_config()
        return {
            "name": self.name,
            "description": self.description,
            "api_key_configured": bool(api_key),
            "model": model,
            "timeout_sec": timeout_sec,
            "audit_log_path": str(self._audit_log_path or _DEFAULT_AUDIT_LOG_PATH),
            "audit_calls": self._audit_call_count,
            "audit_total_input_tokens": self._audit_total_input_tokens,
            "audit_total_tokens": self._audit_total_tokens,
        }
