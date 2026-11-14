"""Data models for L4D2 AI Director telemetry and decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from director.constants import (
    BOSS_FINALE_STAGES,
    BOSS_NAMES,
    DIAGNOSIS_NAMES,
    DIRECTION_NAMES,
    HORDE_FINALE_STAGES,
    HORDE_NAMES,
    INTENT_NAMES,
    MAX_WAVE_SI,
    ROLE_NAMES,
    BossDirective,
    DirectorState,
    FinaleStage,
    HordeDirective,
    SituationDiagnosis,
    SpawnDirection,
    TacticalIntent,
    TargetRole,
    ZombieClass,
)


_WEAPON_MAX_RESERVE_AMMO: dict[str, int] = {
    "weapon_smg": 650,
    "weapon_smg_silenced": 650,
    "weapon_smg_mp5": 650,
    "weapon_pumpshotgun": 56,
    "weapon_shotgun_chrome": 56,
    "weapon_autoshotgun": 90,
    "weapon_shotgun_spas": 90,
    "weapon_hunting_rifle": 150,
    "weapon_sniper_military": 180,
    "weapon_sniper_awp": 180,
    "weapon_sniper_scout": 180,
    "weapon_rifle_m60": 150,
    "weapon_grenade_launcher": 30,
}


def compute_weapon_ammo_pct(weapon_name: str, clip_ammo: int, reserve_ammo: int) -> float:
    """Compute primary ammo reserve+clip percentage from raw weapon telemetry."""
    cls = weapon_name.strip().lower()
    if not cls:
        return 100.0
    clip = max(0, int(clip_ammo))
    reserve = max(0, int(reserve_ammo))
    if cls == "weapon_rifle_m60":
        return min(100.0, (float(clip) / 150.0) * 100.0)
    max_reserve = _WEAPON_MAX_RESERVE_AMMO.get(cls, 360)
    if max_reserve <= 0:
        return 100.0
    return min(100.0, (float(reserve + clip) / float(max_reserve)) * 100.0)


def compute_horde_scale_mult(horde_directive: int) -> float:
    """Map HordeDirective enum to engine common infected horde multiplier."""
    hdir = int(horde_directive)
    if hdir == HordeDirective.SCALE_05X:
        return 0.5
    if hdir == HordeDirective.SCALE_12X:
        return 1.0
    if hdir == HordeDirective.SCALE_16X:
        return 1.25
    if hdir == HordeDirective.RIOT_SQUAD:
        return 1.0
    return 0.8


def compute_tank_parameters(boss_directive: int, alive_count: int) -> tuple[int, int]:
    """Compute scaled Tank HP and extra Tank count from BossDirective and team size."""
    alive = max(1, int(alive_count)) if int(alive_count) > 0 else 4
    base_hp = 5000 + (alive * 800)
    bdir = int(boss_directive)
    if bdir == BossDirective.ADD_2_TANKS:
        return (round(base_hp * 0.75), 2)
    if bdir == BossDirective.ADD_1_TANK:
        return (base_hp, 1)
    return (base_hp, 0)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or isinstance(value, bool):
            return int(value) if isinstance(value, bool) else default
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


@dataclass
class SurvivorTelemetry:
    client: int
    userid: int = 0
    name: str = ""
    alive: bool = True
    incap_or_hanging: bool = False
    black_and_white: bool = False
    pinned: bool = False
    perm_hp: int = 100
    temp_hp: float = 0.0
    eff_hp: float = 100.0
    flow_dist: float = 0.0
    weapon_name: str = ""
    clip_ammo: int = 0
    reserve_ammo: int = 0
    ammo_pct: float = 100.0
    has_medkit: bool = False
    has_throwable: bool = False
    kills_si: int = 0
    kills_ci: int = 0
    headshots: int = 0
    revives_done: int = 0
    pins_cleared: int = 0
    damage_dealt_si: int = 0
    damage_taken: int = 0
    friendly_fire_done: int = 0
    incap_count: int = 0
    impact_score: float = 1.0
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SurvivorTelemetry:
        kills_si = _to_int(data.get("kills_si"), 0)
        revives_done = _to_int(data.get("revives_done"), 0)
        pins_cleared = _to_int(data.get("pins_cleared"), 0)
        dmg_si = _to_int(data.get("damage_dealt_si"), 0)
        default_impact = max(1.0, float(kills_si * 3 + revives_done * 5 + pins_cleared * 4) + float(dmg_si) / 100.0)

        perm_hp = _to_int(data.get("perm_hp"), 100)
        temp_hp = max(0.0, _to_float(data.get("temp_hp"), 0.0))
        incap = _to_bool(data.get("incap_or_hanging"), False)
        alive = _to_bool(data.get("alive"), True)
        default_eff_hp = 0.0 if not alive else (10.0 if incap else float(perm_hp) + temp_hp * 0.5)

        weapon_name = _to_str(data.get("weapon_name"), "")
        clip_ammo = _to_int(data.get("clip_ammo"), 0)
        reserve_ammo = _to_int(data.get("reserve_ammo"), 0)
        if "ammo_pct" in data:
            ammo_pct = _to_float(data.get("ammo_pct"), 100.0)
        elif "weapon_name" in data:
            ammo_pct = compute_weapon_ammo_pct(weapon_name, clip_ammo, reserve_ammo)
        else:
            ammo_pct = 100.0

        return cls(
            client=_to_int(data.get("client"), 0),
            userid=_to_int(data.get("userid"), 0),
            name=_to_str(data.get("name"), ""),
            alive=alive,
            incap_or_hanging=incap,
            black_and_white=_to_bool(data.get("black_and_white"), False),
            pinned=_to_bool(data.get("pinned"), False),
            perm_hp=perm_hp,
            temp_hp=temp_hp,
            eff_hp=_to_float(data.get("eff_hp"), default_eff_hp),
            flow_dist=_to_float(data.get("flow_dist"), 0.0),
            weapon_name=weapon_name,
            clip_ammo=clip_ammo,
            reserve_ammo=reserve_ammo,
            ammo_pct=ammo_pct,
            has_medkit=_to_bool(data.get("has_medkit"), False),
            has_throwable=_to_bool(data.get("has_throwable"), False),
            kills_si=kills_si,
            kills_ci=_to_int(data.get("kills_ci"), 0),
            headshots=_to_int(data.get("headshots"), 0),
            revives_done=revives_done,
            pins_cleared=pins_cleared,
            damage_dealt_si=dmg_si,
            damage_taken=_to_int(data.get("damage_taken"), 0),
            friendly_fire_done=_to_int(data.get("friendly_fire_done"), 0),
            incap_count=_to_int(data.get("incap_count"), 0),
            impact_score=_to_float(data.get("impact_score"), default_impact),
            pos=(
                _to_float(data.get("pos_x"), 0.0),
                _to_float(data.get("pos_y"), 0.0),
                _to_float(data.get("pos_z"), 0.0),
            ),
        )


def _parse_survivors(raw_survivors: Any) -> list[SurvivorTelemetry]:
    result: list[SurvivorTelemetry] = []
    if isinstance(raw_survivors, list):
        for item in raw_survivors:
            if isinstance(item, dict):
                result.append(SurvivorTelemetry.from_dict(item))
    elif isinstance(raw_survivors, dict):
        count = _to_int(raw_survivors.get("count"), 0)
        if count > 0:
            for idx in range(count):
                item = raw_survivors.get(str(idx))
                if isinstance(item, dict):
                    result.append(SurvivorTelemetry.from_dict(item))
        else:
            numeric_keys = sorted(
                (k for k in raw_survivors.keys() if k.isdigit()),
                key=lambda k: int(k),
            )
            for key in numeric_keys:
                item = raw_survivors.get(key)
                if isinstance(item, dict):
                    result.append(SurvivorTelemetry.from_dict(item))
    return result


def _parse_active_si_counts(raw_counts: Any) -> dict[int, int]:
    counts = {zc: 0 for zc in range(1, 7)}
    if isinstance(raw_counts, dict):
        for key, value in raw_counts.items():
            try:
                zc = int(key)
            except (TypeError, ValueError):
                continue
            if 1 <= zc <= 6:
                counts[zc] = max(0, _to_int(value, 0))
    elif isinstance(raw_counts, list):
        for idx, value in enumerate(raw_counts, start=1):
            if 1 <= idx <= 6:
                counts[idx] = max(0, _to_int(value, 0))
    return counts


@dataclass
class TelemetrySnapshot:
    eval_seq: int = 0
    reason: str = ""
    map_name: str = ""
    game_time: float = 0.0
    director_state: int = int(DirectorState.EVALUATE)
    wave_number: int = 0
    last_wave_intent: int = int(TacticalIntent.MAINTAIN_GAUNTLET_FLOW)

    alive_count: int = 0
    down_or_dead_count: int = 0
    avg_eff_hp: float = 0.0
    max_spread_dist: float = 0.0
    front_gap_dist: float = 0.0
    rear_gap_dist: float = 0.0
    p1_front: int = 0
    p4_rear: int = 0
    p_mvp: int = 0
    p_vulnerable: int = 0
    mvp_score_ratio: float = 1.0
    mvp_damage_share: float = 0.0
    mvp_kills_si: int = 0

    black_and_white_count: int = 0
    team_ammo_avg_pct: float = 100.0
    medkits_total: int = 0
    throwables_total: int = 0

    cur_finale_stage: int = int(FinaleStage.FINALE_NONE)
    finale_active: bool = False
    panic_event_active: bool = False
    tank_alive: bool = False
    last_wave_clear_duration: float = 0.0
    wipe_count: int = 0
    wave_damage_taken: int = 0
    wave_incap_count: int = 0
    peak_flow_ratio: float = 0.0
    flow_velocity: float = 0.0
    stagnation_seconds: float = 0.0

    min_cooldown: int = 10
    max_cooldown: int = 35
    max_wave_si: int = MAX_WAVE_SI
    client_count: int = 0
    active_si_total: int = 0
    active_si_counts: dict[int, int] = field(default_factory=lambda: {i: 0 for i in range(1, 7)})
    survivors: list[SurvivorTelemetry] = field(default_factory=list)

    @property
    def is_native_boss_finale_stage(self) -> bool:
        return self.cur_finale_stage in BOSS_FINALE_STAGES

    @property
    def is_native_horde_finale_stage(self) -> bool:
        return self.cur_finale_stage in HORDE_FINALE_STAGES

    @classmethod
    def from_rpc_params(cls, params: Any) -> TelemetrySnapshot:
        if isinstance(params, list):
            data = params[0] if params and isinstance(params[0], dict) else {}
        elif isinstance(params, dict):
            data = params
        else:
            data = {}

        survivors = _parse_survivors(data.get("survivors"))
        active_si_counts = _parse_active_si_counts(data.get("active_si_counts"))
        active_si_total = _to_int(data.get("active_si_total"), sum(active_si_counts.values()))

        # Always compute survivor-derived defaults when alive_survivors is non-empty,
        # falling back to explicit top-level keys only when present in data.
        alive_survivors = [s for s in survivors if s.alive]
        down_or_dead_default = sum(1 for s in survivors if (not s.alive or s.incap_or_hanging))

        if alive_survivors:
            sorted_surv = sorted(alive_survivors, key=lambda s: s.flow_dist, reverse=True)
            alive_count_default = len(sorted_surv)
            avg_eff_hp_default = sum(s.eff_hp for s in sorted_surv) / float(alive_count_default)
            p1_front_default = sorted_surv[0].client
            p4_rear_default = sorted_surv[-1].client

            front_gap_default = 0.0
            rear_gap_default = 0.0
            max_spread_default = 0.0
            if alive_count_default >= 2:
                front_gap_default = max(
                    sorted_surv[0].flow_dist - sorted_surv[1].flow_dist,
                    math.dist(sorted_surv[0].pos, sorted_surv[1].pos),
                )
                rear_gap_default = max(
                    sorted_surv[-2].flow_dist - sorted_surv[-1].flow_dist,
                    math.dist(sorted_surv[-1].pos, sorted_surv[-2].pos),
                )
                for i in range(alive_count_default - 1):
                    for j in range(i + 1, alive_count_default):
                        d = math.dist(sorted_surv[i].pos, sorted_surv[j].pos)
                        if d > max_spread_default:
                            max_spread_default = d

            best_mvp = max(sorted_surv, key=lambda s: s.impact_score)
            avg_score = sum(s.impact_score for s in sorted_surv) / float(alive_count_default)
            mvp_ratio_default = (best_mvp.impact_score / avg_score) if avg_score > 0.1 else 1.0
            total_si_dmg = sum(s.damage_dealt_si for s in survivors)
            mvp_dmg_share_default = (float(best_mvp.damage_dealt_si) / float(total_si_dmg)) if total_si_dmg > 0 else 0.0

            def _vuln_key(s: SurvivorTelemetry) -> float:
                v = s.eff_hp - float(s.incap_count * 20)
                if s.black_and_white:
                    v -= 30.0
                return v

            p_vuln_default = min(sorted_surv, key=_vuln_key).client
            bw_count_default = sum(1 for s in sorted_surv if s.black_and_white)
            ammo_avg_default = sum(s.ammo_pct for s in sorted_surv) / float(alive_count_default)
            medkits_default = sum(1 for s in sorted_surv if s.has_medkit)
            throwables_default = sum(1 for s in sorted_surv if s.has_throwable)
            mvp_kills_default = best_mvp.kills_si
            p_mvp_default = best_mvp.client
        else:
            alive_count_default = 0
            avg_eff_hp_default = 0.0
            p1_front_default = 0
            p4_rear_default = 0
            p_mvp_default = 0
            p_vuln_default = 0
            front_gap_default = 0.0
            rear_gap_default = 0.0
            max_spread_default = 0.0
            mvp_ratio_default = 1.0
            mvp_dmg_share_default = 0.0
            mvp_kills_default = 0
            bw_count_default = 0
            ammo_avg_default = 100.0
            medkits_default = 0
            throwables_default = 0

        alive_count = _to_int(data["alive_count"], alive_count_default) if "alive_count" in data else alive_count_default
        avg_eff_hp = _to_float(data["avg_eff_hp"], avg_eff_hp_default) if "avg_eff_hp" in data else avg_eff_hp_default
        p1_front = _to_int(data["p1_front"], p1_front_default) if "p1_front" in data else p1_front_default
        p4_rear = _to_int(data["p4_rear"], p4_rear_default) if "p4_rear" in data else p4_rear_default
        p_mvp = _to_int(data["p_mvp"], p_mvp_default) if "p_mvp" in data else p_mvp_default
        p_vuln = _to_int(data["p_vulnerable"], p_vuln_default) if "p_vulnerable" in data else p_vuln_default
        front_gap = _to_float(data["front_gap_dist"], front_gap_default) if "front_gap_dist" in data else front_gap_default
        rear_gap = _to_float(data["rear_gap_dist"], rear_gap_default) if "rear_gap_dist" in data else rear_gap_default
        max_spread = _to_float(data["max_spread_dist"], max_spread_default) if "max_spread_dist" in data else max_spread_default
        mvp_ratio = _to_float(data["mvp_score_ratio"], mvp_ratio_default) if "mvp_score_ratio" in data else mvp_ratio_default
        mvp_dmg_share = _to_float(data["mvp_damage_share"], mvp_dmg_share_default) if "mvp_damage_share" in data else mvp_dmg_share_default
        mvp_kills = _to_int(data["mvp_kills_si"], mvp_kills_default) if "mvp_kills_si" in data else mvp_kills_default
        bw_count = _to_int(data["black_and_white_count"], bw_count_default) if "black_and_white_count" in data else bw_count_default
        ammo_avg = _to_float(data["team_ammo_avg_pct"], ammo_avg_default) if "team_ammo_avg_pct" in data else ammo_avg_default
        medkits = _to_int(data["medkits_total"], medkits_default) if "medkits_total" in data else medkits_default
        throwables = _to_int(data["throwables_total"], throwables_default) if "throwables_total" in data else throwables_default

        cur_finale_stage = _to_int(data.get("cur_finale_stage"), int(FinaleStage.FINALE_NONE))
        finale_active = _to_bool(data.get("finale_active"), False)
        if cur_finale_stage == int(FinaleStage.FINALE_NONE):
            finale_active = False

        return cls(
            eval_seq=_to_int(data.get("eval_seq"), 0),
            reason=_to_str(data.get("reason"), ""),
            map_name=_to_str(data.get("map_name"), ""),
            game_time=_to_float(data.get("game_time"), 0.0),
            director_state=_to_int(data.get("director_state"), int(DirectorState.EVALUATE)),
            wave_number=_to_int(data.get("wave_number"), 0),
            last_wave_intent=_to_int(data.get("last_wave_intent"), int(TacticalIntent.MAINTAIN_GAUNTLET_FLOW)),
            alive_count=alive_count,
            down_or_dead_count=_to_int(data.get("down_or_dead_count"), down_or_dead_default),
            avg_eff_hp=avg_eff_hp,
            max_spread_dist=max_spread,
            front_gap_dist=front_gap,
            rear_gap_dist=rear_gap,
            p1_front=p1_front,
            p4_rear=p4_rear,
            p_mvp=p_mvp,
            p_vulnerable=p_vuln,
            mvp_score_ratio=mvp_ratio,
            mvp_damage_share=mvp_dmg_share,
            mvp_kills_si=mvp_kills,
            black_and_white_count=bw_count,
            team_ammo_avg_pct=ammo_avg,
            medkits_total=medkits,
            throwables_total=throwables,
            cur_finale_stage=cur_finale_stage,
            finale_active=finale_active,
            panic_event_active=_to_bool(data.get("panic_event_active"), False),
            tank_alive=_to_bool(data.get("tank_alive"), False),
            last_wave_clear_duration=_to_float(data.get("last_wave_clear_duration"), 0.0),
            wipe_count=_to_int(data.get("wipe_count"), 0),
            wave_damage_taken=_to_int(data.get("wave_damage_taken"), 0),
            wave_incap_count=_to_int(data.get("wave_incap_count"), 0),
            peak_flow_ratio=_to_float(data.get("peak_flow_ratio"), 0.0),
            flow_velocity=_to_float(data.get("flow_velocity"), 0.0),
            stagnation_seconds=_to_float(data.get("stagnation_seconds"), 0.0),
            min_cooldown=_to_int(data.get("min_cooldown"), 10),
            max_cooldown=_to_int(data.get("max_cooldown"), 35),
            max_wave_si=_to_int(data.get("max_wave_si"), MAX_WAVE_SI),
            client_count=_to_int(data.get("client_count"), 0),
            active_si_total=active_si_total,
            active_si_counts=active_si_counts,
            survivors=survivors,
        )


@dataclass
class WavePlan:
    eval_seq: int = 0
    strategy: str = "offline_sm"
    skip_wave: bool = False
    diagnosis: int = int(SituationDiagnosis.HEALTHY_STEADY_PUSH)
    intent: int = int(TacticalIntent.MAINTAIN_GAUNTLET_FLOW)
    target_role: int = int(TargetRole.P1_FRONT)
    target_survivor: int = 0
    spawn_dir: int = int(SpawnDirection.FRONT)
    horde_directive: int = int(HordeDirective.KEEP_NATIVE)
    boss_directive: int = int(BossDirective.RESPECT_NATIVE)
    freeze_ambush: bool = False
    focus_strike: bool = False
    cooldown: int = 20
    wave_target_count: int = 4
    wave_queue: list[int] = field(default_factory=list)
    clear_panic_flag: bool = False
    decay_scores: bool = True
    reset_mob_timer: bool = True
    trigger_rush_victim: bool = False
    spawn_riot_squad: bool = False
    spawn_mercy_clown: bool = False
    alive_survivors: int = 4
    horde_scale_mult: float = -1.0
    tank_hp: int = 0
    extra_tanks: int = -1
    focus_squad_size: int = -1
    suppress_cooldown_mobs: bool = False
    reason: str = ""

    def to_rpc_dict(self) -> dict[str, Any]:
        diag = max(0, min(int(self.diagnosis), len(DIAGNOSIS_NAMES) - 1))
        intent = max(0, min(int(self.intent), len(INTENT_NAMES) - 1))
        role = max(0, min(int(self.target_role), len(ROLE_NAMES) - 1))
        sdir = max(0, min(int(self.spawn_dir), len(DIRECTION_NAMES) - 1))
        hdir = max(0, min(int(self.horde_directive), len(HORDE_NAMES) - 1))
        bdir = max(0, min(int(self.boss_directive), len(BOSS_NAMES) - 1))
        queue = [int(x) for x in self.wave_queue[:MAX_WAVE_SI]]
        target_count = 0 if self.skip_wave else len(queue)

        horde_mult = float(self.horde_scale_mult) if self.horde_scale_mult >= 0.0 else compute_horde_scale_mult(hdir)
        horde_mult = round(horde_mult, 3)
        # sm-ext-rpc re-serializes whole-number floats (e.g. 1.0 -> "1") during reply HMAC verification;
        # nudge integer-valued floats by 0.001 so JSON.GetFloat() and HMAC both match.
        if horde_mult.is_integer():
            horde_mult = round(horde_mult + 0.001, 3)
        default_tank_hp, default_extra_tanks = compute_tank_parameters(bdir, self.alive_survivors)
        tank_hp_val = int(self.tank_hp) if self.tank_hp > 0 else default_tank_hp
        extra_tanks_val = int(self.extra_tanks) if self.extra_tanks >= 0 else default_extra_tanks

        if self.focus_squad_size >= 0:
            focus_sq_val = int(self.focus_squad_size)
        elif self.skip_wave or not self.focus_strike or target_count <= 0:
            focus_sq_val = 0
        else:
            focus_sq_val = max(1, target_count // 2)

        suppress_mobs_val = bool(
            self.suppress_cooldown_mobs or intent == TacticalIntent.MERCY_RELAX_ALLOW_HEAL
        )

        return {
            "ok": True,
            "eval_seq": int(self.eval_seq),
            "strategy": str(self.strategy),
            "skip_wave": bool(self.skip_wave),
            "diagnosis": diag,
            "diagnosis_name": DIAGNOSIS_NAMES[diag],
            "intent": intent,
            "intent_name": INTENT_NAMES[intent],
            "target_role": role,
            "target_role_name": ROLE_NAMES[role],
            "target_survivor": int(self.target_survivor),
            "spawn_dir": sdir,
            "spawn_dir_name": DIRECTION_NAMES[sdir],
            "horde_directive": hdir,
            "horde_directive_name": HORDE_NAMES[hdir],
            "boss_directive": bdir,
            "boss_directive_name": BOSS_NAMES[bdir],
            "horde_scale_mult": horde_mult,
            "tank_hp": tank_hp_val,
            "extra_tanks": extra_tanks_val,
            "focus_squad_size": focus_sq_val,
            "suppress_cooldown_mobs": suppress_mobs_val,
            "freeze_ambush": bool(self.freeze_ambush),
            "focus_strike": bool(self.focus_strike),
            "cooldown": int(self.cooldown),
            "wave_target_count": int(target_count),
            "wave_queue": queue,
            "clear_panic_flag": bool(self.clear_panic_flag),
            "decay_scores": bool(self.decay_scores),
            "reset_mob_timer": bool(self.reset_mob_timer),
            "trigger_rush_victim": bool(self.trigger_rush_victim),
            "spawn_riot_squad": bool(self.spawn_riot_squad),
            "spawn_mercy_clown": bool(self.spawn_mercy_clown),
            "reason": str(self.reason),
        }


@dataclass
class FinaleBossDecision:
    eval_seq: int = 0
    finale_type: int = int(FinaleStage.FINALE_NONE)
    strategy: str = "offline_sm"
    diagnosis: int = int(SituationDiagnosis.FINALE_BOSS_STAGE)
    boss_directive: int = int(BossDirective.RESPECT_NATIVE)
    alive_survivors: int = 4
    tank_hp: int = 0
    extra_tanks: int = -1
    reason: str = ""

    def to_rpc_dict(self) -> dict[str, Any]:
        diag = max(0, min(int(self.diagnosis), len(DIAGNOSIS_NAMES) - 1))
        bdir = max(0, min(int(self.boss_directive), len(BOSS_NAMES) - 1))
        default_tank_hp, default_extra_tanks = compute_tank_parameters(bdir, self.alive_survivors)
        tank_hp_val = int(self.tank_hp) if self.tank_hp > 0 else default_tank_hp
        extra_tanks_val = int(self.extra_tanks) if self.extra_tanks >= 0 else default_extra_tanks
        return {
            "ok": True,
            "eval_seq": int(self.eval_seq),
            "finale_type": int(self.finale_type),
            "strategy": str(self.strategy),
            "diagnosis": diag,
            "diagnosis_name": DIAGNOSIS_NAMES[diag],
            "boss_directive": bdir,
            "boss_directive_name": BOSS_NAMES[bdir],
            "tank_hp": tank_hp_val,
            "extra_tanks": extra_tanks_val,
            "reason": str(self.reason),
        }
