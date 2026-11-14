"""100% faithful Python port of l4d2_llm_director.sp deterministic State-Machine."""

from __future__ import annotations

import logging
import random
from typing import Any, Callable

from director.constants import (
    MAX_WAVE_SI,
    BossDirective,
    HordeDirective,
    SituationDiagnosis,
    SpawnDirection,
    TacticalIntent,
    TargetRole,
)
from director.guardrails import build_wave_composition, clamp_int
from director.models import (
    FinaleBossDecision,
    TelemetrySnapshot,
    WavePlan,
    compute_tank_parameters,
)
from director.rpc_client import schedule_director_command
from director.strategies import BaseDirectorStrategy

_LOGGER = logging.getLogger("l4d2_director_rpc")
_RESET_EVENTS = frozenset({
    "round_start",
    "map_start",
    "map_end",
    "mission_lost",
    "map_transition",
    "round_end",
})


class OfflineStateMachineStrategy(BaseDirectorStrategy):
    """Exact replica of l4d2_llm_director.sp v0.3.0_sm tactical engine."""

    name = "offline_sm"
    description = "Deterministic State-Machine Director ported 1:1 from l4d2_llm_director.sp"

    def __init__(
        self,
        rng: random.Random | None = None,
        command_dispatcher: Callable[..., Any] | None = None,
    ) -> None:
        self._rng = rng
        self._command_dispatcher = command_dispatcher or schedule_director_command
        self._tank_seen_this_round = False
        self._pity_tank_triggered = False

    def on_event(self, event_name: str, payload: dict[str, Any]) -> None:
        if event_name in _RESET_EVENTS:
            self._tank_seen_this_round = False
            self._pity_tank_triggered = False
        elif event_name in ("tank_spawned", "tank_killed"):
            self._tank_seen_this_round = True

    def _select_pity_tank_rule(self, s: TelemetrySnapshot) -> tuple[int, int, int]:
        if (
            s.avg_eff_hp > 75.0
            and s.wipe_count == 0
            and (
                s.team_ammo_avg_pct > 80.0
                or s.medkits_total >= 1
                or (0.0 < s.last_wave_clear_duration <= 4.0)
            )
        ):
            boss_dir = int(BossDirective.ADD_2_TANKS)
        elif s.avg_eff_hp >= 55.0 and s.wipe_count == 0:
            boss_dir = int(BossDirective.ADD_1_TANK)
        else:
            boss_dir = int(BossDirective.RESPECT_NATIVE)

        tank_hp, extra_tanks = compute_tank_parameters(boss_dir, s.alive_count)
        return boss_dir, tank_hp, extra_tanks

    def on_tick(self, snapshot: TelemetrySnapshot) -> None:
        if snapshot.tank_alive:
            self._tank_seen_this_round = True
            return
        if self._tank_seen_this_round or self._pity_tank_triggered:
            return
        if snapshot.finale_active or snapshot.alive_count <= 0 or snapshot.peak_flow_ratio < 75.0:
            return

        self._pity_tank_triggered = True
        self._tank_seen_this_round = True
        boss_dir, tank_hp, extra_tanks = self._select_pity_tank_rule(snapshot)
        target = snapshot.p1_front if snapshot.p1_front > 0 else 0
        _LOGGER.info(
            "75%% flow pity Tank triggered [map=%s flow=%.1f%%]: boss_dir=%d total_tanks=%d hp=%d target=%d",
            snapshot.map_name,
            snapshot.peak_flow_ratio,
            boss_dir,
            1 + extra_tanks,
            tank_hp,
            target,
        )
        self._command_dispatcher(
            "spawn_tank",
            target_survivor=target,
            boss_directive=extra_tanks,
            message=str(tank_hp),
        )

    def _rand_bit(self) -> int:
        if self._rng is not None:
            return self._rng.randint(0, 1)
        return random.randint(0, 1)

    def evaluate_finale_boss_stage(
        self,
        snapshot: TelemetrySnapshot,
        finale_type: int,
        arg: str = "",
    ) -> FinaleBossDecision:
        """Exact port of EvaluateFinaleBossStageDirective() (lines 1597-1645)."""
        s = snapshot
        if (
            s.down_or_dead_count >= 2
            or s.avg_eff_hp < 35.0
            or s.black_and_white_count >= 2
            or s.team_ammo_avg_pct < 25.0
        ):
            boss_dir = BossDirective.BLOCK_EXTRA_RESPAWN
        elif s.wipe_count >= 2 or s.avg_eff_hp < 55.0 or s.down_or_dead_count >= 1:
            boss_dir = BossDirective.RESPECT_NATIVE
        elif (
            s.avg_eff_hp > 75.0
            and s.wipe_count == 0
            and (
                s.team_ammo_avg_pct > 80.0
                or s.medkits_total >= 1
                or (0.0 < s.last_wave_clear_duration <= 4.0)
            )
        ):
            boss_dir = BossDirective.ADD_2_TANKS
        elif s.avg_eff_hp >= 55.0:
            boss_dir = BossDirective.ADD_1_TANK
        else:
            boss_dir = BossDirective.RESPECT_NATIVE

        return FinaleBossDecision(
            eval_seq=s.eval_seq,
            finale_type=int(finale_type),
            strategy=self.name,
            diagnosis=int(SituationDiagnosis.FINALE_BOSS_STAGE),
            boss_directive=int(boss_dir),
            alive_survivors=int(s.alive_count),
            reason=f"finale_stage={finale_type} arg={arg}".strip(),
        )

    def evaluate_wave(self, snapshot: TelemetrySnapshot) -> WavePlan:
        """Exact port of ExecuteEvaluationAndPlanWave() (lines 2617-3004)."""
        s = snapshot
        if s.tank_alive:
            self._tank_seen_this_round = True
        if s.alive_count == 0:
            return WavePlan(
                eval_seq=s.eval_seq,
                strategy=self.name,
                skip_wave=True,
                cooldown=10,
                wave_target_count=0,
                wave_queue=[],
                alive_survivors=int(s.alive_count),
                decay_scores=False,
                reset_mob_timer=False,
                reason="No alive survivors",
            )

        max_allowed_si = min(s.max_wave_si, MAX_WAVE_SI)
        base_target_si = min(s.alive_count + 2, max_allowed_si)

        is_critical_fatigue = (
            s.down_or_dead_count >= 2
            or s.avg_eff_hp < 35.0
            or s.black_and_white_count >= 2
            or (s.wipe_count >= 2 and s.avg_eff_hp < 55.0)
            or (s.wave_incap_count >= 2 and s.avg_eff_hp < 50.0)
            or (s.wave_damage_taken >= 260 and s.avg_eff_hp < 50.0)
            or (s.team_ammo_avg_pct < 20.0 and s.avg_eff_hp < 50.0)
        )

        clear_panic_flag = False

        if is_critical_fatigue and s.last_wave_intent != TacticalIntent.MERCY_RELAX_ALLOW_HEAL:
            diagnosis = SituationDiagnosis.TEAM_CRITICAL_EXHAUSTED
            intent = TacticalIntent.MERCY_RELAX_ALLOW_HEAL
            target_role = TargetRole.P_VULNERABLE
            target_survivor = s.p_vulnerable
            spawn_dir = SpawnDirection.FRONT
            horde_dir = HordeDirective.SCALE_05X
            boss_dir = BossDirective.BLOCK_EXTRA_RESPAWN
            freeze_ambush = False
            focus_strike = False
            wave_target_count = 1 if (s.down_or_dead_count >= 2 or s.avg_eff_hp < 25.0) else 2
            cooldown = 35 if (s.down_or_dead_count >= 2 or s.wipe_count >= 2) else 26
            branch_reason = "critical_fatigue_mercy"

        elif is_critical_fatigue and s.last_wave_intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL:
            diagnosis = SituationDiagnosis.HEALTHY_STEADY_PUSH
            intent = TacticalIntent.MAINTAIN_GAUNTLET_FLOW
            target_role = TargetRole.P1_FRONT
            target_survivor = s.p1_front
            spawn_dir = SpawnDirection.MIDDLE_FLANK
            horde_dir = HordeDirective.SCALE_05X
            boss_dir = BossDirective.RESPECT_NATIVE
            freeze_ambush = False
            focus_strike = False
            wave_target_count = clamp_int(s.alive_count + 1, 2, max_allowed_si)
            cooldown = 20
            branch_reason = "mercy_chain_prevented"

        elif s.is_native_boss_finale_stage or s.tank_alive:
            diagnosis = SituationDiagnosis.FINALE_BOSS_STAGE
            intent = (
                TacticalIntent.ESCALATE_CLIMAX
                if (s.avg_eff_hp >= 70.0 and s.down_or_dead_count == 0)
                else TacticalIntent.MAINTAIN_GAUNTLET_FLOW
            )
            target_role = TargetRole.P_MVP
            target_survivor = s.p_mvp
            spawn_dir = SpawnDirection.MIDDLE_FLANK
            horde_dir = HordeDirective.SCALE_05X if s.tank_alive else HordeDirective.KEEP_NATIVE

            if s.wipe_count >= 2 or s.avg_eff_hp < 55.0 or s.down_or_dead_count >= 1:
                boss_dir = BossDirective.RESPECT_NATIVE
            elif (
                s.avg_eff_hp > 75.0
                and s.wipe_count == 0
                and (
                    s.team_ammo_avg_pct > 80.0
                    or s.medkits_total >= 1
                    or (0.0 < s.last_wave_clear_duration <= 4.0)
                )
            ):
                boss_dir = BossDirective.ADD_2_TANKS
            elif s.avg_eff_hp >= 55.0:
                boss_dir = BossDirective.ADD_1_TANK
            else:
                boss_dir = BossDirective.RESPECT_NATIVE

            freeze_ambush = False
            focus_strike = s.avg_eff_hp >= 60.0 and s.down_or_dead_count == 0
            wave_target_count = clamp_int(s.alive_count, 2, max_allowed_si)
            cooldown = 22
            branch_reason = "finale_boss_or_tank_alive"

        elif s.panic_event_active or s.is_native_horde_finale_stage or s.finale_active:
            diagnosis = SituationDiagnosis.NATIVE_INFINITE_RUSH
            intent = TacticalIntent.MAINTAIN_GAUNTLET_FLOW
            target_role = TargetRole.P1_FRONT
            target_survivor = s.p1_front
            spawn_dir = SpawnDirection.MIDDLE_FLANK
            horde_dir = HordeDirective.SCALE_05X if s.avg_eff_hp < 55.0 else HordeDirective.KEEP_NATIVE
            if s.is_native_horde_finale_stage or s.finale_active:
                boss_dir = BossDirective.RESPECT_NATIVE
            else:
                boss_dir = (
                    BossDirective.ADD_1_TANK
                    if (s.avg_eff_hp >= 72.0 and s.wipe_count == 0)
                    else BossDirective.RESPECT_NATIVE
                )
            freeze_ambush = False
            focus_strike = True
            wave_target_count = clamp_int(s.alive_count + 1, 3, max_allowed_si)
            cooldown = 22
            clear_panic_flag = True
            branch_reason = "panic_or_finale_horde"

        elif s.front_gap_dist >= 600.0 and s.alive_count >= 2:
            diagnosis = SituationDiagnosis.SPEEDRUNNER_ISOLATED
            intent = TacticalIntent.SNIPE_LONE_WOLF
            target_role = TargetRole.P1_FRONT
            target_survivor = s.p1_front
            spawn_dir = SpawnDirection.FRONT
            horde_dir = HordeDirective.KEEP_NATIVE
            boss_dir = BossDirective.RESPECT_NATIVE
            freeze_ambush = True
            focus_strike = True
            wave_target_count = base_target_si
            cooldown = 15
            branch_reason = "speedrunner_isolated"

        elif s.rear_gap_dist >= 600.0 and s.alive_count >= 2:
            diagnosis = SituationDiagnosis.REAR_STRAGGLER_EXPOSED
            intent = TacticalIntent.SNIPE_LONE_WOLF
            target_role = TargetRole.P4_REAR
            target_survivor = s.p4_rear
            spawn_dir = SpawnDirection.BEHIND
            horde_dir = HordeDirective.KEEP_NATIVE
            boss_dir = BossDirective.RESPECT_NATIVE
            freeze_ambush = False
            focus_strike = True
            wave_target_count = base_target_si
            cooldown = 15
            branch_reason = "rear_straggler_exposed"

        elif s.stagnation_seconds >= 25.0 or (
            s.flow_velocity < 1.5 and s.wave_number >= 1 and s.max_spread_dist < 380.0
        ):
            diagnosis = SituationDiagnosis.CORNER_CAMPING_STRONG
            intent = TacticalIntent.BREAK_CAMPING_FORMATION
            target_role = TargetRole.P_VULNERABLE
            target_survivor = s.p_vulnerable
            spawn_dir = SpawnDirection.ABOVE_ROOF if self._rand_bit() == 0 else SpawnDirection.MIDDLE_FLANK
            horde_dir = HordeDirective.SCALE_12X
            boss_dir = BossDirective.RESPECT_NATIVE
            freeze_ambush = False
            focus_strike = True
            wave_target_count = base_target_si
            cooldown = 15
            branch_reason = "corner_camping_strong"

        elif (
            (s.mvp_score_ratio >= 1.55 or (s.mvp_damage_share > 0.40 and s.mvp_kills_si >= 2))
            and s.wave_number >= 1
            and s.avg_eff_hp >= 55.0
        ):
            diagnosis = SituationDiagnosis.MVP_PLAYER_CARRYING
            intent = TacticalIntent.ASSASSINATE_MVP
            target_role = TargetRole.P_MVP
            target_survivor = s.p_mvp
            spawn_dir = SpawnDirection.MIDDLE_FLANK
            horde_dir = HordeDirective.SCALE_12X
            boss_dir = (
                BossDirective.ADD_1_TANK
                if (s.avg_eff_hp >= 78.0 and s.wipe_count == 0 and not s.finale_active)
                else BossDirective.RESPECT_NATIVE
            )
            freeze_ambush = True
            focus_strike = True
            wave_target_count = base_target_si
            cooldown = 20
            branch_reason = "mvp_player_carrying"

        elif s.avg_eff_hp > 75.0 and (
            s.team_ammo_avg_pct > 80.0 or (0.0 < s.last_wave_clear_duration <= 4.0)
        ):
            diagnosis = SituationDiagnosis.HEALTHY_STEADY_PUSH
            intent = TacticalIntent.ESCALATE_CLIMAX
            target_role = TargetRole.P1_FRONT
            target_survivor = s.p1_front
            dir_roll = s.wave_number % 4
            if dir_roll == 0:
                spawn_dir = SpawnDirection.FRONT
            elif dir_roll == 1:
                spawn_dir = SpawnDirection.MIDDLE_FLANK
            elif dir_roll == 2:
                spawn_dir = SpawnDirection.ABOVE_ROOF
            else:
                spawn_dir = SpawnDirection.BEHIND

            horde_dir = (
                HordeDirective.RIOT_SQUAD
                if (s.avg_eff_hp >= 80.0 and (s.wave_number % 3 == 2))
                else HordeDirective.SCALE_16X
            )
            boss_dir = (
                BossDirective.ADD_2_TANKS
                if (s.wipe_count == 0 and not s.finale_active)
                else BossDirective.RESPECT_NATIVE
            )
            freeze_ambush = True
            focus_strike = True
            wave_target_count = base_target_si
            cooldown = 14
            branch_reason = "escalate_climax"

        else:
            diagnosis = SituationDiagnosis.HEALTHY_STEADY_PUSH
            intent = TacticalIntent.MAINTAIN_GAUNTLET_FLOW
            target_role = TargetRole.P1_FRONT
            target_survivor = s.p1_front
            dir_roll = s.wave_number % 4
            if dir_roll == 0:
                spawn_dir = SpawnDirection.FRONT
            elif dir_roll == 1:
                spawn_dir = SpawnDirection.MIDDLE_FLANK
            elif dir_roll == 2:
                spawn_dir = SpawnDirection.ABOVE_ROOF
            else:
                spawn_dir = SpawnDirection.BEHIND

            horde_dir = HordeDirective.SCALE_12X
            boss_dir = (
                BossDirective.ADD_1_TANK
                if (s.avg_eff_hp >= 55.0 and s.wipe_count == 0 and not s.finale_active)
                else BossDirective.RESPECT_NATIVE
            )
            freeze_ambush = spawn_dir in (SpawnDirection.FRONT, SpawnDirection.ABOVE_ROOF)
            focus_strike = s.avg_eff_hp >= 60.0
            wave_target_count = base_target_si
            cooldown = 20
            branch_reason = "standard_gauntlet_flow"

        cooldown = clamp_int(cooldown, s.min_cooldown, s.max_cooldown)
        if wave_target_count > max_allowed_si:
            wave_target_count = max_allowed_si

        wave_queue = build_wave_composition(
            int(intent),
            wave_target_count,
            s.alive_count,
            s.active_si_counts,
            rng=self._rng,
        )

        return WavePlan(
            eval_seq=s.eval_seq,
            strategy=self.name,
            skip_wave=False,
            diagnosis=int(diagnosis),
            intent=int(intent),
            target_role=int(target_role),
            target_survivor=int(target_survivor),
            spawn_dir=int(spawn_dir),
            horde_directive=int(horde_dir),
            boss_directive=int(boss_dir),
            freeze_ambush=bool(freeze_ambush),
            focus_strike=bool(focus_strike),
            cooldown=int(cooldown),
            wave_target_count=len(wave_queue),
            wave_queue=wave_queue,
            alive_survivors=int(s.alive_count),
            clear_panic_flag=clear_panic_flag,
            decay_scores=True,
            reset_mob_timer=(intent != TacticalIntent.BREAK_CAMPING_FORMATION),
            trigger_rush_victim=(intent == TacticalIntent.BREAK_CAMPING_FORMATION),
            spawn_riot_squad=(horde_dir == HordeDirective.RIOT_SQUAD),
            spawn_mercy_clown=(intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL),
            reason=branch_reason,
        )
