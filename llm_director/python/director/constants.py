"""Spec v1.2.0 enums and constants matching l4d2_llm_director.sp."""

from enum import IntEnum


class ZombieClass(IntEnum):
    NONE = 0
    SMOKER = 1
    BOOMER = 2
    HUNTER = 3
    SPITTER = 4
    JOCKEY = 5
    CHARGER = 6
    WITCH = 7
    TANK = 8


class DirectorState(IntEnum):
    DORMANT = 0
    COOLDOWN = 1
    EVALUATE = 2
    SPAWNING = 3
    COMBAT = 4


class SituationDiagnosis(IntEnum):
    HEALTHY_STEADY_PUSH = 0
    SPEEDRUNNER_ISOLATED = 1
    REAR_STRAGGLER_EXPOSED = 2
    MVP_PLAYER_CARRYING = 3
    CORNER_CAMPING_STRONG = 4
    TEAM_CRITICAL_EXHAUSTED = 5
    NATIVE_INFINITE_RUSH = 6
    FINALE_BOSS_STAGE = 7


class TacticalIntent(IntEnum):
    ESCALATE_CLIMAX = 0
    SNIPE_LONE_WOLF = 1
    ASSASSINATE_MVP = 2
    BREAK_CAMPING_FORMATION = 3
    MERCY_RELAX_ALLOW_HEAL = 4
    MAINTAIN_GAUNTLET_FLOW = 5


class TargetRole(IntEnum):
    NONE = 0
    P1_FRONT = 1
    P4_REAR = 2
    P_MVP = 3
    P_VULNERABLE = 4


class SpawnDirection(IntEnum):
    FRONT = 0
    MIDDLE_FLANK = 1
    BEHIND = 2
    ABOVE_ROOF = 3
    BELOW_FLOOR = 4


class HordeDirective(IntEnum):
    KEEP_NATIVE = 0
    SCALE_05X = 1
    SCALE_12X = 2
    SCALE_16X = 3
    RIOT_SQUAD = 4


class BossDirective(IntEnum):
    RESPECT_NATIVE = 0
    ADD_1_TANK = 1
    ADD_2_TANKS = 2
    BLOCK_EXTRA_RESPAWN = 3


class FinaleStage(IntEnum):
    FINALE_GAUNTLET_1 = 0
    FINALE_HORDE_ATTACK_1 = 1
    FINALE_HALFTIME_BOSS = 2
    FINALE_GAUNTLET_2 = 3
    FINALE_HORDE_ATTACK_2 = 4
    FINALE_FINAL_BOSS = 5
    FINALE_HORDE_ESCAPE = 6
    FINALE_CUSTOM_PANIC = 7
    FINALE_CUSTOM_TANK = 8
    FINALE_CUSTOM_SCRIPTED = 9
    FINALE_CUSTOM_DELAY = 10
    FINALE_CUSTOM_CLEAROUT = 11
    FINALE_GAUNTLET_START = 12
    FINALE_GAUNTLET_HORDE = 13
    FINALE_GAUNTLET_HORDE_BONUSTIME = 14
    FINALE_GAUNTLET_BOSS_INCOMING = 15
    FINALE_GAUNTLET_BOSS = 16
    FINALE_GAUNTLET_ESCAPE = 17
    FINALE_NONE = 18


BOSS_FINALE_STAGES = frozenset({
    FinaleStage.FINALE_HALFTIME_BOSS,
    FinaleStage.FINALE_FINAL_BOSS,
    FinaleStage.FINALE_CUSTOM_TANK,
    FinaleStage.FINALE_GAUNTLET_BOSS_INCOMING,
    FinaleStage.FINALE_GAUNTLET_BOSS,
})

HORDE_FINALE_STAGES = frozenset({
    FinaleStage.FINALE_GAUNTLET_1,
    FinaleStage.FINALE_HORDE_ATTACK_1,
    FinaleStage.FINALE_GAUNTLET_2,
    FinaleStage.FINALE_HORDE_ATTACK_2,
    FinaleStage.FINALE_HORDE_ESCAPE,
    FinaleStage.FINALE_CUSTOM_PANIC,
    FinaleStage.FINALE_GAUNTLET_HORDE,
    FinaleStage.FINALE_GAUNTLET_ESCAPE,
})

HARD_CC_CLASSES = frozenset({
    ZombieClass.SMOKER,
    ZombieClass.HUNTER,
    ZombieClass.JOCKEY,
    ZombieClass.CHARGER,
})

MAX_WAVE_SI = 12

STATE_NAMES = (
    "DORMANT",
    "COOLDOWN",
    "EVALUATE",
    "SPAWNING",
    "COMBAT",
)

DIAGNOSIS_NAMES = (
    "HEALTHY_STEADY_PUSH",
    "SPEEDRUNNER_ISOLATED",
    "REAR_STRAGGLER_EXPOSED",
    "MVP_PLAYER_CARRYING",
    "CORNER_CAMPING_STRONG",
    "TEAM_CRITICAL_EXHAUSTED",
    "NATIVE_INFINITE_RUSH",
    "FINALE_BOSS_STAGE",
)

INTENT_NAMES = (
    "ESCALATE_CLIMAX",
    "SNIPE_LONE_WOLF",
    "ASSASSINATE_MVP",
    "BREAK_CAMPING_FORMATION",
    "MERCY_RELAX_ALLOW_HEAL",
    "MAINTAIN_GAUNTLET_FLOW",
)

ROLE_NAMES = (
    "NONE",
    "P1_FRONT",
    "P4_REAR",
    "P_MVP",
    "P_VULNERABLE",
)

DIRECTION_NAMES = (
    "FRONT",
    "MIDDLE_FLANK",
    "BEHIND",
    "ABOVE_ROOF",
    "BELOW_FLOOR",
)

HORDE_NAMES = (
    "KEEP_NATIVE",
    "SCALE_0.5X",
    "SCALE_1.2X",
    "SCALE_1.6X",
    "RIOT_SQUAD",
)

BOSS_NAMES = (
    "RESPECT_NATIVE",
    "ADD_1_TANK",
    "ADD_2_TANKS",
    "BLOCK_EXTRA_RESPAWN",
)

SI_NAMES = (
    "None",
    "Smoker",
    "Boomer",
    "Hunter",
    "Spitter",
    "Jockey",
    "Charger",
    "Witch",
    "Tank",
)

SI_CMD_NAMES = (
    "",
    "smoker",
    "boomer",
    "hunter",
    "spitter",
    "jockey",
    "charger",
    "witch",
    "tank",
)
