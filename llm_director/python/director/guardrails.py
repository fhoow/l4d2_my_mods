"""Spec Section 5 guardrails shared across all Python Director strategies."""

from __future__ import annotations

import math
import random

from director.constants import (
    HARD_CC_CLASSES,
    MAX_WAVE_SI,
    BossDirective,
    HordeDirective,
    SituationDiagnosis,
    SpawnDirection,
    TacticalIntent,
    TargetRole,
    ZombieClass,
)
from director.models import TelemetrySnapshot, WavePlan


def clamp_int(val: int, min_val: int, max_val: int) -> int:
    if val < min_val:
        return min_val
    if val > max_val:
        return max_val
    return val


def is_hard_cc_class(zc: int) -> bool:
    return int(zc) in HARD_CC_CLASSES


def compute_target_si_limit(alive_count: int, max_wave_si: int = MAX_WAVE_SI) -> int:
    """Spec Section 5.1: target_si_limit = alive_survivors + 2, capped by max_wave_si <= 12."""
    max_allowed = min(max(1, int(max_wave_si)), MAX_WAVE_SI)
    return min(max(1, int(alive_count)) + 2, max_allowed)


def enforce_anti_hard_cc_cap(
    requested_class: int,
    alive_survivors: int,
    active_si_by_class: dict[int, int],
    current_queue: list[int],
) -> int:
    """Spec Section 5.2 Anti-Hard-CC Class Cap matching EnforceAntiHardCCCap()."""
    zc = int(requested_class)
    if zc < ZombieClass.SMOKER or zc > ZombieClass.CHARGER:
        zc = ZombieClass.SPITTER if len(current_queue) % 2 == 0 else ZombieClass.BOOMER

    if not is_hard_cc_class(zc):
        return zc

    max_per_class = max(1, math.ceil(float(max(1, alive_survivors)) / 3.0))
    current_count = int(active_si_by_class.get(zc, 0)) + sum(1 for item in current_queue if item == zc)

    if current_count < max_per_class:
        return zc

    return int(ZombieClass.SPITTER if (len(current_queue) % 2 == 0) else ZombieClass.BOOMER)


def build_wave_composition(
    intent: int,
    count: int,
    alive_survivors: int,
    active_si_by_class: dict[int, int],
    rng: random.Random | None = None,
) -> list[int]:
    """Match BuildWaveComposition() in l4d2_llm_director.sp."""
    r = rng if rng is not None else random
    queue: list[int] = []

    for i in range(max(0, int(count))):
        if intent in (TacticalIntent.SNIPE_LONE_WOLF, TacticalIntent.ASSASSINATE_MVP):
            pool = (
                ZombieClass.HUNTER,
                ZombieClass.JOCKEY,
                ZombieClass.CHARGER,
                ZombieClass.SMOKER,
                ZombieClass.SPITTER,
                ZombieClass.BOOMER,
            )
            desired = int(pool[i % len(pool)])
        elif intent == TacticalIntent.BREAK_CAMPING_FORMATION:
            pool = (
                ZombieClass.CHARGER,
                ZombieClass.SPITTER,
                ZombieClass.BOOMER,
                ZombieClass.JOCKEY,
                ZombieClass.SMOKER,
                ZombieClass.SPITTER,
            )
            desired = int(pool[i % len(pool)])
        elif intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL:
            pool = (ZombieClass.BOOMER, ZombieClass.SPITTER)
            desired = int(pool[i % len(pool)])
        elif intent == TacticalIntent.ESCALATE_CLIMAX:
            pool = (
                ZombieClass.CHARGER,
                ZombieClass.SPITTER,
                ZombieClass.HUNTER,
                ZombieClass.JOCKEY,
                ZombieClass.SMOKER,
                ZombieClass.BOOMER,
            )
            desired = int(pool[i % len(pool)])
        else:
            pool = (
                ZombieClass.HUNTER,
                ZombieClass.SPITTER,
                ZombieClass.BOOMER,
                ZombieClass.JOCKEY,
                ZombieClass.SMOKER,
                ZombieClass.CHARGER,
            )
            idx = (i + r.randint(0, 1)) % len(pool)
            desired = int(pool[idx])

        queue.append(enforce_anti_hard_cc_cap(desired, alive_survivors, active_si_by_class, queue))

    return queue


def resolve_target_survivor(role: int, snapshot: TelemetrySnapshot) -> int:
    if role == TargetRole.P1_FRONT and snapshot.p1_front > 0:
        return snapshot.p1_front
    if role == TargetRole.P4_REAR and snapshot.p4_rear > 0:
        return snapshot.p4_rear
    if role == TargetRole.P_MVP and snapshot.p_mvp > 0:
        return snapshot.p_mvp
    if role == TargetRole.P_VULNERABLE and snapshot.p_vulnerable > 0:
        return snapshot.p_vulnerable

    for candidate in (snapshot.p1_front, snapshot.p_mvp, snapshot.p_vulnerable, snapshot.p4_rear):
        if candidate > 0:
            return candidate
    for surv in snapshot.survivors:
        if surv.alive and surv.client > 0:
            return surv.client
    return 0


def validate_and_sanitize_wave_plan(
    plan: WavePlan,
    snapshot: TelemetrySnapshot,
    rng: random.Random | None = None,
) -> WavePlan:
    """Enforce Spec Section 5 & 6 guardrails on any strategy output (especially LLM)."""
    plan.eval_seq = snapshot.eval_seq

    if snapshot.alive_count <= 0 or plan.skip_wave:
        plan.skip_wave = True
        plan.cooldown = clamp_int(plan.cooldown or 10, 5, snapshot.max_cooldown)
        plan.wave_target_count = 0
        plan.wave_queue = []
        return plan

    plan.diagnosis = clamp_int(int(plan.diagnosis), 0, SituationDiagnosis.FINALE_BOSS_STAGE)
    plan.intent = clamp_int(int(plan.intent), 0, TacticalIntent.MAINTAIN_GAUNTLET_FLOW)
    plan.target_role = clamp_int(int(plan.target_role), 0, TargetRole.P_VULNERABLE)
    plan.spawn_dir = clamp_int(int(plan.spawn_dir), 0, SpawnDirection.BELOW_FLOOR)
    plan.horde_directive = clamp_int(int(plan.horde_directive), 0, HordeDirective.RIOT_SQUAD)
    plan.boss_directive = clamp_int(int(plan.boss_directive), 0, BossDirective.BLOCK_EXTRA_RESPAWN)

    # Prevent consecutive Mercy Relax waves
    if (
        plan.intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL
        and snapshot.last_wave_intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL
    ):
        plan.intent = TacticalIntent.MAINTAIN_GAUNTLET_FLOW
        plan.diagnosis = int(SituationDiagnosis.HEALTHY_STEADY_PUSH)
        plan.boss_directive = int(BossDirective.RESPECT_NATIVE)

    # Spec Section 6.2 Rule #1: Lock boss = RESPECT_NATIVE during non-Boss Finale stages
    if snapshot.is_native_horde_finale_stage or (snapshot.finale_active and not snapshot.is_native_boss_finale_stage):
        if plan.boss_directive in (BossDirective.ADD_1_TANK, BossDirective.ADD_2_TANKS):
            plan.boss_directive = int(BossDirective.RESPECT_NATIVE)

    # Mercy wave rules
    if plan.intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL:
        plan.boss_directive = int(BossDirective.BLOCK_EXTRA_RESPAWN)
        plan.focus_strike = False
        plan.freeze_ambush = False

    max_allowed = min(max(1, snapshot.max_wave_si), MAX_WAVE_SI)
    desired_count = clamp_int(int(plan.wave_target_count), 1, max_allowed)
    plan.cooldown = clamp_int(int(plan.cooldown), snapshot.min_cooldown, snapshot.max_cooldown)

    if plan.target_survivor <= 0:
        plan.target_survivor = resolve_target_survivor(plan.target_role, snapshot)

    # Validate / sanitize wave_queue through Anti-Hard-CC cap
    raw_queue = list(plan.wave_queue) if plan.wave_queue else []
    if len(raw_queue) < desired_count:
        generated = build_wave_composition(
            plan.intent,
            desired_count,
            snapshot.alive_count,
            snapshot.active_si_counts,
            rng=rng,
        )
        raw_queue.extend(generated[len(raw_queue):])

    sanitized_queue: list[int] = []
    for raw_zc in raw_queue[:desired_count]:
        zc = int(raw_zc)
        if plan.intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL and is_hard_cc_class(zc):
            zc = int(ZombieClass.BOOMER if len(sanitized_queue) % 2 == 0 else ZombieClass.SPITTER)
        sanitized_queue.append(
            enforce_anti_hard_cc_cap(zc, snapshot.alive_count, snapshot.active_si_counts, sanitized_queue)
        )

    plan.wave_queue = sanitized_queue
    plan.wave_target_count = len(sanitized_queue)
    plan.alive_survivors = int(snapshot.alive_count)
    plan.decay_scores = True
    plan.reset_mob_timer = plan.intent != TacticalIntent.BREAK_CAMPING_FORMATION
    plan.trigger_rush_victim = plan.intent == TacticalIntent.BREAK_CAMPING_FORMATION
    plan.spawn_riot_squad = plan.horde_directive == HordeDirective.RIOT_SQUAD
    plan.spawn_mercy_clown = plan.intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL
    return plan
