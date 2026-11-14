"""Unit and live TCP RPC integration tests for the Python AI Director."""

from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PYTHON_DIR = Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from director.constants import (
    BossDirective,
    FinaleStage,
    HordeDirective,
    SituationDiagnosis,
    SpawnDirection,
    TacticalIntent,
    TargetRole,
    ZombieClass,
)
from director.guardrails import (
    build_wave_composition,
    enforce_anti_hard_cc_cap,
    validate_and_sanitize_wave_plan,
)
from director.models import TelemetrySnapshot, WavePlan
from director.rpc_client import (
    build_director_command_payload,
    compute_hmac,
    dump_compact,
    send_director_command,
    sign_reply,
    sign_request,
)
from director.strategies.gemini_decision_strategy import GeminiDecisionStrategy
from director.strategies.llm_strategy import LLMDirectorStrategy
from director.strategies.offline_state_machine import OfflineStateMachineStrategy
from rpc_server import (
    DEFAULT_SECRET,
    DirectorRpcRuntime,
    handle_client,
    process_raw_request,
)


def make_snapshot(**overrides) -> TelemetrySnapshot:
    base = {
        "eval_seq": 1,
        "reason": "test",
        "map_name": "c2m1_highway",
        "game_time": 120.0,
        "director_state": 2,
        "wave_number": 2,
        "last_wave_intent": int(TacticalIntent.MAINTAIN_GAUNTLET_FLOW),
        "alive_count": 4,
        "down_or_dead_count": 0,
        "avg_eff_hp": 65.0,
        "max_spread_dist": 450.0,
        "front_gap_dist": 200.0,
        "rear_gap_dist": 200.0,
        "p1_front": 1,
        "p4_rear": 4,
        "p_mvp": 2,
        "p_vulnerable": 3,
        "mvp_score_ratio": 1.1,
        "mvp_damage_share": 0.28,
        "mvp_kills_si": 1,
        "black_and_white_count": 0,
        "team_ammo_avg_pct": 70.0,
        "medkits_total": 1,
        "throwables_total": 2,
        "cur_finale_stage": int(FinaleStage.FINALE_NONE),
        "finale_active": False,
        "panic_event_active": False,
        "tank_alive": False,
        "last_wave_clear_duration": 10.0,
        "wipe_count": 0,
        "wave_damage_taken": 40,
        "wave_incap_count": 0,
        "peak_flow_ratio": 35.0,
        "flow_velocity": 12.0,
        "stagnation_seconds": 5.0,
        "min_cooldown": 10,
        "max_cooldown": 35,
        "max_wave_si": 12,
        "client_count": 8,
        "active_si_total": 0,
        "active_si_counts": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0},
    }
    base.update(overrides)
    return TelemetrySnapshot.from_rpc_params(base)


class TestOfflineStateMachineParity(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = OfflineStateMachineStrategy(rng=random.Random(42))

    def test_zero_alive_survivors_skips_wave(self) -> None:
        plan = self.strategy.evaluate_wave(make_snapshot(alive_count=0))
        self.assertTrue(plan.skip_wave)
        self.assertEqual(plan.cooldown, 10)
        self.assertEqual(plan.wave_queue, [])

    def test_critical_fatigue_triggers_mercy_relax(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(down_or_dead_count=2, avg_eff_hp=30.0, p_vulnerable=3)
        )
        self.assertFalse(plan.skip_wave)
        self.assertEqual(plan.diagnosis, SituationDiagnosis.TEAM_CRITICAL_EXHAUSTED)
        self.assertEqual(plan.intent, TacticalIntent.MERCY_RELAX_ALLOW_HEAL)
        self.assertEqual(plan.target_role, TargetRole.P_VULNERABLE)
        self.assertEqual(plan.target_survivor, 3)
        self.assertEqual(plan.horde_directive, HordeDirective.SCALE_05X)
        self.assertEqual(plan.boss_directive, BossDirective.BLOCK_EXTRA_RESPAWN)
        self.assertEqual(plan.wave_target_count, 1)
        self.assertEqual(plan.cooldown, 35)
        self.assertTrue(plan.spawn_mercy_clown)
        for zc in plan.wave_queue:
            self.assertIn(zc, (ZombieClass.BOOMER, ZombieClass.SPITTER))

    def test_mercy_chain_prevention(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(
                down_or_dead_count=2,
                avg_eff_hp=30.0,
                last_wave_intent=int(TacticalIntent.MERCY_RELAX_ALLOW_HEAL),
            )
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.HEALTHY_STEADY_PUSH)
        self.assertEqual(plan.intent, TacticalIntent.MAINTAIN_GAUNTLET_FLOW)
        self.assertEqual(plan.boss_directive, BossDirective.RESPECT_NATIVE)
        self.assertEqual(plan.wave_target_count, 5)
        self.assertEqual(plan.cooldown, 20)

    def test_finale_boss_stage_and_additive_tanks(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(
                cur_finale_stage=int(FinaleStage.FINALE_FINAL_BOSS),
                finale_active=True,
                avg_eff_hp=85.0,
                team_ammo_avg_pct=90.0,
            )
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.FINALE_BOSS_STAGE)
        self.assertEqual(plan.intent, TacticalIntent.ESCALATE_CLIMAX)
        self.assertEqual(plan.boss_directive, BossDirective.ADD_2_TANKS)
        self.assertEqual(plan.wave_target_count, 4)

        decision = self.strategy.evaluate_finale_boss_stage(
            make_snapshot(avg_eff_hp=65.0, team_ammo_avg_pct=50.0, medkits_total=0, wipe_count=0),
            int(FinaleStage.FINALE_HALFTIME_BOSS),
        )
        self.assertEqual(decision.boss_directive, BossDirective.ADD_1_TANK)

    def test_panic_and_non_boss_finale_lock(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(
                cur_finale_stage=int(FinaleStage.FINALE_HORDE_ATTACK_1),
                finale_active=True,
                panic_event_active=True,
                avg_eff_hp=85.0,
            )
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.NATIVE_INFINITE_RUSH)
        self.assertEqual(plan.boss_directive, BossDirective.RESPECT_NATIVE)
        self.assertTrue(plan.clear_panic_flag)
        self.assertEqual(plan.wave_target_count, 5)

    def test_speedrunner_isolated(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(front_gap_dist=720.0, p1_front=2)
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.SPEEDRUNNER_ISOLATED)
        self.assertEqual(plan.intent, TacticalIntent.SNIPE_LONE_WOLF)
        self.assertEqual(plan.target_role, TargetRole.P1_FRONT)
        self.assertEqual(plan.target_survivor, 2)
        self.assertEqual(plan.spawn_dir, SpawnDirection.FRONT)
        self.assertTrue(plan.freeze_ambush)
        self.assertTrue(plan.focus_strike)
        self.assertEqual(plan.wave_target_count, 6)
        self.assertEqual(plan.cooldown, 15)

    def test_rear_straggler_exposed(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(rear_gap_dist=650.0, p4_rear=4)
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.REAR_STRAGGLER_EXPOSED)
        self.assertEqual(plan.target_role, TargetRole.P4_REAR)
        self.assertEqual(plan.target_survivor, 4)
        self.assertEqual(plan.spawn_dir, SpawnDirection.BEHIND)
        self.assertFalse(plan.freeze_ambush)

    def test_corner_camping_strong(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(stagnation_seconds=26.0, p_vulnerable=3)
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.CORNER_CAMPING_STRONG)
        self.assertEqual(plan.intent, TacticalIntent.BREAK_CAMPING_FORMATION)
        self.assertTrue(plan.trigger_rush_victim)
        self.assertFalse(plan.reset_mob_timer)

    def test_mvp_carrying(self) -> None:
        plan = self.strategy.evaluate_wave(
            make_snapshot(mvp_score_ratio=1.7, avg_eff_hp=80.0, p_mvp=1)
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.MVP_PLAYER_CARRYING)
        self.assertEqual(plan.intent, TacticalIntent.ASSASSINATE_MVP)
        self.assertEqual(plan.target_survivor, 1)
        self.assertEqual(plan.boss_directive, BossDirective.ADD_1_TANK)
        self.assertTrue(plan.freeze_ambush)

    def test_escalate_climax_and_riot_squad(self) -> None:
        # wave_number=2 -> 2 % 3 == 2 -> HORDE_RIOT_SQUAD + dir_roll=2 (ABOVE_ROOF)
        plan = self.strategy.evaluate_wave(
            make_snapshot(avg_eff_hp=85.0, team_ammo_avg_pct=88.0, wave_number=2)
        )
        self.assertEqual(plan.diagnosis, SituationDiagnosis.HEALTHY_STEADY_PUSH)
        self.assertEqual(plan.intent, TacticalIntent.ESCALATE_CLIMAX)
        self.assertEqual(plan.spawn_dir, SpawnDirection.ABOVE_ROOF)
        self.assertEqual(plan.horde_directive, HordeDirective.RIOT_SQUAD)
        self.assertTrue(plan.spawn_riot_squad)
        self.assertEqual(plan.boss_directive, BossDirective.ADD_2_TANKS)
        self.assertEqual(plan.cooldown, 14)

    def test_asymmetric_scaling_and_anti_hard_cc_cap(self) -> None:
        # 4 survivors -> target_si = 6; maxPerClass = ceil(4/3) = 2
        q4 = build_wave_composition(
            TacticalIntent.ESCALATE_CLIMAX,
            count=6,
            alive_survivors=4,
            active_si_by_class={i: 0 for i in range(1, 7)},
            rng=random.Random(1),
        )
        self.assertEqual(len(q4), 6)
        for cls_id in (ZombieClass.SMOKER, ZombieClass.HUNTER, ZombieClass.JOCKEY, ZombieClass.CHARGER):
            self.assertLessEqual(q4.count(cls_id), 2)

        # 10 survivors -> target_si = 12; maxPerClass = ceil(10/3) = 4
        plan10 = self.strategy.evaluate_wave(make_snapshot(alive_count=10, max_wave_si=12))
        self.assertEqual(plan10.wave_target_count, 12)
        for cls_id in (ZombieClass.SMOKER, ZombieClass.HUNTER, ZombieClass.JOCKEY, ZombieClass.CHARGER):
            self.assertLessEqual(plan10.wave_queue.count(cls_id), 4)

        # If 2 Hunters are already alive for 4 survivors, requesting another Hunter converts to Spitter/Boomer
        converted = enforce_anti_hard_cc_cap(
            ZombieClass.HUNTER,
            alive_survivors=4,
            active_si_by_class={int(ZombieClass.HUNTER): 2},
            current_queue=[],
        )
        self.assertIn(converted, (ZombieClass.SPITTER, ZombieClass.BOOMER))


class TestLLMStrategyAndGuardrails(unittest.TestCase):
    def test_llm_fallback_when_unconfigured(self) -> None:
        llm = LLMDirectorStrategy()
        snap = make_snapshot(front_gap_dist=700.0)
        plan = llm.evaluate_wave(snap)
        self.assertEqual(plan.diagnosis, SituationDiagnosis.SPEEDRUNNER_ISOLATED)
        self.assertIn("fallback_offline_sm", plan.strategy)

    def test_llm_guardrails_sanitize_excess_hard_cc_and_finale_boss(self) -> None:
        snap = make_snapshot(
            alive_count=4,
            cur_finale_stage=int(FinaleStage.FINALE_GAUNTLET_1),
            finale_active=True,
            last_wave_intent=int(TacticalIntent.MERCY_RELAX_ALLOW_HEAL),
        )
        unsafe_plan = WavePlan(
            eval_seq=snap.eval_seq,
            strategy="llm_hybrid",
            diagnosis=int(SituationDiagnosis.TEAM_CRITICAL_EXHAUSTED),
            intent=int(TacticalIntent.MERCY_RELAX_ALLOW_HEAL),  # consecutive mercy illegal!
            boss_directive=int(BossDirective.ADD_2_TANKS),      # illegal on non-boss finale!
            wave_target_count=6,
            wave_queue=[ZombieClass.HUNTER] * 6,                # illegal 6 hunters for 4 survivors!
            cooldown=999,
        )
        safe = validate_and_sanitize_wave_plan(unsafe_plan, snap)
        self.assertNotEqual(safe.intent, TacticalIntent.MERCY_RELAX_ALLOW_HEAL)
        self.assertEqual(safe.boss_directive, BossDirective.RESPECT_NATIVE)
        self.assertLessEqual(safe.wave_queue.count(ZombieClass.HUNTER), 2)
        self.assertEqual(safe.cooldown, snap.max_cooldown)


class TestRpcProtocolAndLiveTcpRoundTrip(unittest.IsolatedAsyncioTestCase):
    def test_cpp_float_raw_slice_hmac_verification(self) -> None:
        # Simulate C++ nlohmann::json::dump() with float formatting `80.30000305175781` and `1e-05`
        params_raw = '{"alive_count":4,"avg_eff_hp":80.30000305175781,"flow_velocity":1e-05}'
        ts = int(time.time())
        req_id = '"cpp-req-1"'
        payload = '"2.0"' + '"EvaluateWave"' + params_raw + str(ts) + req_id
        sig = compute_hmac(payload, DEFAULT_SECRET)
        raw_line = (
            f'{{"id":{req_id},"jsonrpc":"2.0","method":"EvaluateWave",'
            f'"params":{params_raw},"sig":"{sig}","timestamp":{ts}}}'
        )
        runtime = DirectorRpcRuntime()
        resp = process_raw_request(raw_line, DEFAULT_SECRET, runtime=runtime)
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertIn("result", resp)
        self.assertTrue(resp["result"]["ok"])

    async def test_live_tcp_rpc_roundtrip(self) -> None:
        runtime = DirectorRpcRuntime(default_strategy="offline_sm")
        server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, DEFAULT_SECRET, runtime),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]

        async def rpc_call(method: str, params, expect_reply: bool = True):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                req = {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                    "timestamp": int(time.time()),
                    "id": f"test-{method}" if expect_reply else None,
                }
                req["sig"] = sign_request(req, DEFAULT_SECRET)
                writer.write((dump_compact(req) + "\n").encode("utf-8"))
                await writer.drain()
                if not expect_reply:
                    return None
                line = await asyncio.wait_for(reader.readline(), timeout=3.0)
                reply = json.loads(line.decode("utf-8"))
                self.assertEqual(reply["sig"], sign_reply(reply, DEFAULT_SECRET))
                return reply["result"]
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            ping_res = await rpc_call("Ping", {"source": "unit_test"})
            self.assertTrue(ping_res["pong"])
            self.assertEqual(ping_res["strategy"], "offline_sm")

            echo_res = await rpc_call("Echo", [10, "Hua", "hi"])
            self.assertTrue(echo_res["ok"])

            # Simulate SourceMod SetJSON object structure for survivors + active_si_counts
            sm_payload = {
                "eval_seq": 7,
                "reason": "CooldownTimerElapsed",
                "map_name": "c1m1_hotel",
                "game_time": 42.5,
                "director_state": 2,
                "wave_number": 0,
                "last_wave_intent": 5,
                "alive_count": 4,
                "down_or_dead_count": 0,
                "avg_eff_hp": 90.0,
                "max_spread_dist": 300.0,
                "front_gap_dist": 120.0,
                "rear_gap_dist": 110.0,
                "p1_front": 1,
                "p4_rear": 4,
                "p_mvp": 1,
                "p_vulnerable": 4,
                "mvp_score_ratio": 1.1,
                "mvp_damage_share": 0.25,
                "mvp_kills_si": 0,
                "black_and_white_count": 0,
                "team_ammo_avg_pct": 95.0,
                "medkits_total": 4,
                "throwables_total": 2,
                "cur_finale_stage": 18,
                "finale_active": False,
                "panic_event_active": False,
                "tank_alive": False,
                "last_wave_clear_duration": 0.0,
                "wipe_count": 0,
                "wave_damage_taken": 0,
                "wave_incap_count": 0,
                "peak_flow_ratio": 12.0,
                "flow_velocity": 18.0,
                "stagnation_seconds": 0.0,
                "min_cooldown": 10,
                "max_cooldown": 35,
                "max_wave_si": 12,
                "client_count": 4,
                "active_si_total": 0,
                "active_si_counts": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0},
                "survivors": {
                    "count": 1,
                    "0": {"client": 1, "name": "Nick", "alive": True, "eff_hp": 100.0},
                },
            }

            await rpc_call("OnDirectorEvent", {"event": "round_start", "map_name": "c1m1_hotel"}, expect_reply=False)
            await rpc_call("OnHeartbeatTick", sm_payload, expect_reply=False)

            wave_res = await rpc_call("EvaluateWave", sm_payload)
            required_keys = {
                "ok", "eval_seq", "strategy", "skip_wave", "diagnosis", "diagnosis_name",
                "intent", "intent_name", "target_role", "target_role_name", "target_survivor",
                "spawn_dir", "spawn_dir_name", "horde_directive", "horde_directive_name",
                "boss_directive", "boss_directive_name", "freeze_ambush", "focus_strike",
                "cooldown", "wave_target_count", "wave_queue", "clear_panic_flag",
                "decay_scores", "reset_mob_timer", "trigger_rush_victim",
                "spawn_riot_squad", "spawn_mercy_clown", "reason",
            }
            self.assertTrue(required_keys.issubset(wave_res.keys()))
            self.assertEqual(wave_res["eval_seq"], 7)
            self.assertEqual(wave_res["intent"], TacticalIntent.ESCALATE_CLIMAX)
            self.assertEqual(len(wave_res["wave_queue"]), wave_res["wave_target_count"])

            boss_res = await rpc_call(
                "EvaluateFinaleBossStage",
                {**sm_payload, "finale_type": int(FinaleStage.FINALE_FINAL_BOSS), "finale_arg": ""},
            )
            self.assertEqual(boss_res["boss_directive"], BossDirective.ADD_2_TANKS)

            switch_res = await rpc_call("SetStrategy", {"strategy": "llm_hybrid"})
            self.assertTrue(switch_res["ok"])
            self.assertEqual(switch_res["strategy"], "llm_hybrid")

            reload_res = await rpc_call("ReloadStrategies", {"action": "reload"})
            self.assertTrue(reload_res["ok"])
            self.assertEqual(reload_res["strategy"], "llm_hybrid")

            status_res = await rpc_call("GetStatus", {})
            self.assertTrue(status_res["ok"])
            self.assertEqual(status_res["active_strategy"], "llm_hybrid")
        finally:
            server.close()
            await server.wait_closed()

    async def test_python_to_sourcemod_director_command(self) -> None:
        received: list[dict] = []

        async def mock_sm_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            line = await reader.readline()
            received.append(json.loads(line.decode("utf-8")))
            writer.close()
            await writer.wait_closed()

        sm_server = await asyncio.start_server(mock_sm_handler, "127.0.0.1", 0)
        sm_port = sm_server.sockets[0].getsockname()[1]
        try:
            await send_director_command(
                "127.0.0.1",
                sm_port,
                DEFAULT_SECRET,
                "spawn_si",
                zombie_class=int(ZombieClass.CHARGER),
                target_survivor=2,
                spawn_dir=int(SpawnDirection.MIDDLE_FLANK),
                focus_squad=True,
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(received), 1)
            msg = received[0]
            self.assertEqual(msg["method"], "DirectorCommand")
            self.assertIsInstance(msg["params"], list)
            self.assertEqual(msg["params"][0]["action"], "spawn_si")
            self.assertEqual(msg["params"][0]["zombie_class"], int(ZombieClass.CHARGER))
            self.assertEqual(msg["sig"], sign_request(msg, DEFAULT_SECRET))
        finally:
            sm_server.close()
            await sm_server.wait_closed()


class TestRawTelemetryAggregationAndActuators(unittest.TestCase):
    """Verify Python computes survivor aggregations & actuator params from raw telemetry."""

    def test_raw_survivors_aggregation_and_actuator_outputs(self) -> None:
        from director.models import (
            compute_horde_scale_mult,
            compute_tank_parameters,
            compute_weapon_ammo_pct,
        )

        # Verify weapon reserve ammo helper behaves sensibly for known and unknown weapons.
        self.assertGreater(compute_weapon_ammo_pct("weapon_rifle_ak47", 40, 360), 90.0)
        self.assertEqual(compute_weapon_ammo_pct("weapon_pumpshotgun", 0, 0), 0.0)
        self.assertEqual(compute_horde_scale_mult(int(HordeDirective.SCALE_05X)), 0.5)
        self.assertGreater(compute_horde_scale_mult(int(HordeDirective.SCALE_16X)), 1.0)
        hp_4, extra_4 = compute_tank_parameters(int(BossDirective.ADD_1_TANK), 4)
        self.assertGreater(hp_4, 0)
        self.assertEqual(extra_4, 1)

        # Raw-only payload: omit pre-aggregated fields (alive_count, avg_eff_hp, p1_front,
        # p4_rear, p_mvp, p_vulnerable, mvp_score_ratio, team_ammo_avg_pct, spread/gaps).
        raw_params = {
            "session_id": "test-raw-session",
            "tick_count": 42,
            "map_name": "c2m1_highway",
            "director_state": 3,
            "survivors_outside_saferoom": True,
            "survivors": [
                {
                    "client": 1,
                    "userid": 101,
                    "perm_hp": 90,
                    "temp_hp": 10.0,
                    "alive": True,
                    "incap": False,
                    "pinned": False,
                    "flow_dist": 5000.0,
                    "pos_x": 2000.0,
                    "pos_y": 0.0,
                    "pos_z": 0.0,
                    "weapon_name": "weapon_rifle_ak47",
                    "clip_ammo": 40,
                    "reserve_ammo": 360,
                    "kills_si": 12,
                    "revives_done": 1,
                    "pins_cleared": 3,
                    "damage_dealt_si": 2800.0,
                },
                {
                    "client": 2,
                    "userid": 102,
                    "perm_hp": 80,
                    "temp_hp": 0.0,
                    "alive": True,
                    "incap": False,
                    "pinned": False,
                    "flow_dist": 4500.0,
                    "pos_x": 1500.0,
                    "pos_y": 0.0,
                    "pos_z": 0.0,
                    "weapon_name": "weapon_autoshotgun",
                    "clip_ammo": 10,
                    "reserve_ammo": 90,
                    "kills_si": 3,
                    "revives_done": 0,
                    "pins_cleared": 1,
                    "damage_dealt_si": 600.0,
                },
                {
                    "client": 3,
                    "userid": 103,
                    "perm_hp": 75,
                    "temp_hp": 0.0,
                    "alive": True,
                    "incap": False,
                    "pinned": False,
                    "flow_dist": 4300.0,
                    "pos_x": 1300.0,
                    "pos_y": 0.0,
                    "pos_z": 0.0,
                    "weapon_name": "weapon_smg",
                    "clip_ammo": 50,
                    "reserve_ammo": 325,
                    "kills_si": 2,
                    "revives_done": 0,
                    "pins_cleared": 0,
                    "damage_dealt_si": 400.0,
                },
                {
                    "client": 4,
                    "userid": 104,
                    "perm_hp": 20,
                    "temp_hp": 0.0,
                    "alive": True,
                    "incap": False,
                    "pinned": False,
                    "flow_dist": 3000.0,
                    "pos_x": 0.0,
                    "pos_y": 0.0,
                    "pos_z": 0.0,
                    "weapon_name": "weapon_pumpshotgun",
                    "clip_ammo": 0,
                    "reserve_ammo": 0,
                    "kills_si": 0,
                    "revives_done": 0,
                    "pins_cleared": 0,
                    "damage_dealt_si": 100.0,
                },
            ],
        }

        snap = TelemetrySnapshot.from_rpc_params(raw_params)
        self.assertEqual(snap.alive_count, 4)
        self.assertEqual(snap.p1_front, 1)
        self.assertEqual(snap.p4_rear, 4)
        self.assertEqual(snap.p_mvp, 1)
        self.assertEqual(snap.p_vulnerable, 4)
        self.assertGreater(snap.mvp_score_ratio, 0.4)
        self.assertGreater(snap.avg_eff_hp, 0.0)
        self.assertLess(snap.avg_eff_hp, 100.0)
        self.assertGreater(snap.team_ammo_avg_pct, 0.0)
        self.assertLess(snap.team_ammo_avg_pct, 100.0)
        self.assertGreaterEqual(snap.front_gap_dist, 499.0)
        self.assertGreaterEqual(snap.rear_gap_dist, 1299.0)
        self.assertGreaterEqual(snap.max_spread_dist, 1999.0)

        strategy = OfflineStateMachineStrategy()
        plan = strategy.evaluate_wave(snap)
        self.assertIsInstance(plan, WavePlan)
        self.assertEqual(plan.alive_survivors, 4)
        plan_dict = plan.to_rpc_dict()
        self.assertIsInstance(plan_dict["horde_scale_mult"], float)
        self.assertGreater(plan_dict["horde_scale_mult"], 0.0)
        self.assertIsInstance(plan_dict["tank_hp"], int)
        self.assertGreater(plan_dict["tank_hp"], 0)
        self.assertIsInstance(plan_dict["extra_tanks"], int)
        self.assertGreaterEqual(plan_dict["extra_tanks"], 0)
        self.assertIsInstance(plan_dict["focus_squad_size"], int)
        self.assertGreaterEqual(plan_dict["focus_squad_size"], 0)
        self.assertIsInstance(plan_dict["suppress_cooldown_mobs"], bool)

        boss_decision = strategy.evaluate_finale_boss_stage(snap, finale_type=2, arg="2")
        self.assertEqual(boss_decision.alive_survivors, 4)
        boss_dict = boss_decision.to_rpc_dict()
        self.assertIsInstance(boss_dict["tank_hp"], int)
        self.assertGreater(boss_dict["tank_hp"], 0)
        self.assertIsInstance(boss_dict["extra_tanks"], int)
        self.assertGreaterEqual(boss_dict["extra_tanks"], 0)
        self.assertIsNotNone(boss_dict["boss_directive"])

    def test_finale_none_stage_clears_stale_finale_active_flag(self) -> None:
        snap = TelemetrySnapshot.from_rpc_params(
            {
                "map_name": "c8m1_apartment",
                "cur_finale_stage": int(FinaleStage.FINALE_NONE),
                "finale_active": True,
                "alive_count": 4,
                "avg_eff_hp": 90.0,
                "team_ammo_avg_pct": 95.0,
                "p1_front": 1,
                "p4_rear": 4,
                "p_mvp": 1,
                "p_vulnerable": 4,
            }
        )
        self.assertFalse(snap.finale_active)
        plan = OfflineStateMachineStrategy().evaluate_wave(snap)
        self.assertNotEqual(plan.diagnosis, int(SituationDiagnosis.NATIVE_INFINITE_RUSH))
        self.assertEqual(plan.intent, int(TacticalIntent.ESCALATE_CLIMAX))

    def test_wave_plan_never_emits_integer_valued_float_for_sm_ext_rpc_hmac(self) -> None:
        for mult_in in (-1.0, 1.0, 2.0, 0.0):
            plan = WavePlan(
                horde_directive=int(HordeDirective.SCALE_12X),
                horde_scale_mult=mult_in,
                wave_queue=[1, 2, 3],
            )
            out_mult = plan.to_rpc_dict()["horde_scale_mult"]
            self.assertIsInstance(out_mult, float)
            self.assertFalse(out_mult.is_integer(), f"Expected non-integer float for {mult_in}, got {out_mult}")

    def test_heartbeat_75_percent_pity_tank_trigger_and_reset(self) -> None:
        dispatched: list[tuple[str, dict]] = []

        def fake_dispatch(action: str, **kwargs) -> bool:
            dispatched.append((action, kwargs))
            return True

        strat = OfflineStateMachineStrategy(command_dispatcher=fake_dispatch)

        # Below 75% flow -> no pity tank
        strat.on_tick(
            TelemetrySnapshot.from_rpc_params(
                {"alive_count": 4, "avg_eff_hp": 90.0, "team_ammo_avg_pct": 95.0, "peak_flow_ratio": 74.9, "p1_front": 2}
            )
        )
        self.assertEqual(len(dispatched), 0)

        # Reaches 75% flow with no Tank seen -> triggers once with ADD_2_TANKS rule (extra=2, hp=6150)
        strat.on_tick(
            TelemetrySnapshot.from_rpc_params(
                {"alive_count": 4, "avg_eff_hp": 90.0, "team_ammo_avg_pct": 95.0, "peak_flow_ratio": 75.2, "p1_front": 2}
            )
        )
        self.assertEqual(len(dispatched), 1)
        action, kwargs = dispatched[0]
        self.assertEqual(action, "spawn_tank")
        self.assertEqual(kwargs["target_survivor"], 2)
        self.assertEqual(kwargs["boss_directive"], 2)
        self.assertEqual(kwargs["message"], "6150")

        # Subsequent ticks in same round do not duplicate
        strat.on_tick(
            TelemetrySnapshot.from_rpc_params(
                {"alive_count": 4, "avg_eff_hp": 90.0, "team_ammo_avg_pct": 95.0, "peak_flow_ratio": 85.0, "p1_front": 2}
            )
        )
        self.assertEqual(len(dispatched), 1)

        # New round resets; if a Tank already spawned earlier, 75% does NOT trigger
        strat.on_event("round_start", {})
        strat.on_event("tank_spawned", {})
        strat.on_tick(
            TelemetrySnapshot.from_rpc_params(
                {"alive_count": 4, "avg_eff_hp": 90.0, "team_ammo_avg_pct": 95.0, "peak_flow_ratio": 80.0, "p1_front": 1}
            )
        )
        self.assertEqual(len(dispatched), 1)


class TestGeminiDecisionStrategy(unittest.TestCase):
    def test_fallback_when_api_key_missing(self) -> None:
        with mock.patch("director.strategies.gemini_decision_strategy._load_env_files"), mock.patch.dict(
            "os.environ", {"GEMINI_API_KEY": ""}, clear=False
        ):
            strat = GeminiDecisionStrategy()
            plan = strat.evaluate_wave(make_snapshot())
            self.assertEqual(plan.strategy, "gemini_decision(fallback_no_key)")
            self.assertFalse(plan.skip_wave)

    def test_evaluate_wave_uses_decision_answers_and_strict_scaling(self) -> None:
        from types import SimpleNamespace

        fake_answers = {
            "diagnosis": SimpleNamespace(type="choice", choice="MVP_PLAYER_CARRYING", confidence=0.92),
            "intent": SimpleNamespace(type="choice", choice="ASSASSINATE_MVP", confidence=0.89),
            "target_role": SimpleNamespace(type="choice", choice="P_MVP", confidence=0.95),
            "spawn_dir": SimpleNamespace(type="choice", choice="ABOVE_ROOF", confidence=0.81),
            "horde_directive": SimpleNamespace(type="choice", choice="SCALE_1.6X", confidence=0.88),
            "boss_directive": SimpleNamespace(type="choice", choice="ADD_1_TANK", confidence=0.77),
            "use_freeze_ambush": SimpleNamespace(type="noul", noul=0.91, confidence=0.91),
            "use_focus_strike": SimpleNamespace(type="noul", noul=0.87, confidence=0.87),
            "lead_si_class": SimpleNamespace(type="choice", choice="Hunter", confidence=0.84),
            "support_si_class": SimpleNamespace(type="choice", choice="Spitter", confidence=0.82),
            "aggression_score": SimpleNamespace(type="score", score=4.0, confidence=0.90),
        }
        fake_client = SimpleNamespace(
            decisions=SimpleNamespace(
                create=mock.MagicMock(return_value=SimpleNamespace(answers=fake_answers))
            )
        )

        strat = GeminiDecisionStrategy(client=fake_client)
        snap = make_snapshot(min_cooldown=10, max_cooldown=35, alive_count=4, p_mvp=2)
        plan = strat.evaluate_wave(snap)

        fake_client.decisions.create.assert_called_once()
        call_kwargs = fake_client.decisions.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "gemini-decision-1")
        self.assertIn("diagnosis", call_kwargs["questions"])
        self.assertIn("aggression_score", call_kwargs["questions"])

        self.assertEqual(plan.strategy, "gemini_decision")
        self.assertEqual(plan.diagnosis, SituationDiagnosis.MVP_PLAYER_CARRYING)
        self.assertEqual(plan.intent, TacticalIntent.ASSASSINATE_MVP)
        self.assertEqual(plan.target_role, TargetRole.P_MVP)
        self.assertEqual(plan.target_survivor, 2)
        self.assertEqual(plan.spawn_dir, SpawnDirection.ABOVE_ROOF)
        self.assertEqual(plan.horde_directive, HordeDirective.SCALE_16X)
        self.assertEqual(plan.boss_directive, BossDirective.ADD_1_TANK)
        self.assertTrue(plan.freeze_ambush)
        self.assertTrue(plan.focus_strike)
        # Max aggression (4.0/4.0) drives cooldown down to min_cooldown (10) and full SI limit (4+2=6)
        self.assertEqual(plan.cooldown, 10)
        self.assertEqual(plan.wave_target_count, 6)
        self.assertEqual(plan.wave_queue[:2], [int(ZombieClass.HUNTER), int(ZombieClass.SPITTER)])
        self.assertEqual(len(plan.wave_queue), 6)

    def test_evaluate_finale_boss_stage_and_error_fallback(self) -> None:
        from types import SimpleNamespace

        fake_client = SimpleNamespace(
            decisions=SimpleNamespace(
                create=mock.MagicMock(
                    return_value=SimpleNamespace(
                        answers={
                            "boss_directive": SimpleNamespace(
                                type="choice", choice="ADD_2_TANKS", confidence=0.93
                            )
                        }
                    )
                )
            )
        )
        strat = GeminiDecisionStrategy(client=fake_client)
        decision = strat.evaluate_finale_boss_stage(
            make_snapshot(finale_active=True, cur_finale_stage=int(FinaleStage.FINALE_FINAL_BOSS)),
            int(FinaleStage.FINALE_FINAL_BOSS),
        )
        self.assertEqual(decision.strategy, "gemini_decision")
        self.assertEqual(decision.boss_directive, BossDirective.ADD_2_TANKS)

        # Exception during API call falls back cleanly to offline_sm
        failing_client = SimpleNamespace(
            decisions=SimpleNamespace(create=mock.MagicMock(side_effect=TimeoutError("rpc timeout")))
        )
        failing_strat = GeminiDecisionStrategy(client=failing_client)
        fallback_plan = failing_strat.evaluate_wave(make_snapshot())
        self.assertEqual(fallback_plan.strategy, "gemini_decision(fallback_err:TimeoutError)")

    def test_runtime_set_strategy_gemini_decision(self) -> None:
        rt = DirectorRpcRuntime(default_strategy="offline_sm")
        self.assertIn("gemini_decision", rt.registry.list_names())
        res = rt.handle_method("SetStrategy", {"strategy": "gemini_decision"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["strategy"], "gemini_decision")

    def test_probability_ranked_quota_composition(self) -> None:
        from types import SimpleNamespace

        fake_answers = {
            "diagnosis": SimpleNamespace(type="choice", choice="CORNER_CAMPING_STRONG"),
            "intent": SimpleNamespace(type="choice", choice="BREAK_CAMPING_FORMATION"),
            "target_role": SimpleNamespace(type="choice", choice="P1_FRONT"),
            "spawn_dir": SimpleNamespace(type="choice", choice="FRONT"),
            "horde_directive": SimpleNamespace(type="choice", choice="RIOT_SQUAD"),
            "boss_directive": SimpleNamespace(type="choice", choice="RESPECT_NATIVE"),
            "use_freeze_ambush": SimpleNamespace(type="noul", noul=0.8),
            "use_focus_strike": SimpleNamespace(type="noul", noul=0.3),
            "lead_si_class": SimpleNamespace(
                type="choice",
                choice="Charger",
                probabilities={"Charger": 0.45, "Spitter": 0.30, "Boomer": 0.15, "Smoker": 0.05, "Hunter": 0.03, "Jockey": 0.02},
            ),
            "support_si_class": SimpleNamespace(
                type="choice",
                choice="Spitter",
                probabilities={"Spitter": 0.50, "Boomer": 0.25, "Charger": 0.15, "Smoker": 0.05, "Hunter": 0.03, "Jockey": 0.02},
            ),
            "aggression_score": SimpleNamespace(type="score", score=4.0),
        }
        fake_client = SimpleNamespace(
            decisions=SimpleNamespace(
                create=mock.MagicMock(return_value=SimpleNamespace(answers=fake_answers))
            )
        )
        strat = GeminiDecisionStrategy(client=fake_client)
        plan = strat.evaluate_wave(make_snapshot(alive_count=4))
        # Slots 0,1 = Charger, Spitter; remaining 4 slots follow weighted D'Hondt quota without violating hard-CC cap (max 2 Chargers)
        self.assertEqual(len(plan.wave_queue), 6)
        self.assertEqual(plan.wave_queue[:2], [int(ZombieClass.CHARGER), int(ZombieClass.SPITTER)])
        self.assertLessEqual(plan.wave_queue.count(int(ZombieClass.CHARGER)), 2)
        self.assertIn(int(ZombieClass.BOOMER), plan.wave_queue)
        self.assertGreaterEqual(plan.wave_queue.count(int(ZombieClass.SPITTER)), 2)

    def test_shortcircuit_on_critical_mercy_and_afk(self) -> None:
        from types import SimpleNamespace

        fake_client = SimpleNamespace(
            decisions=SimpleNamespace(create=mock.MagicMock())
        )
        strat = GeminiDecisionStrategy(client=fake_client)

        mercy_plan = strat.evaluate_wave(
            make_snapshot(down_or_dead_count=2, avg_eff_hp=30.0, p_vulnerable=3)
        )
        self.assertEqual(mercy_plan.strategy, "gemini_decision")
        self.assertEqual(mercy_plan.intent, TacticalIntent.MERCY_RELAX_ALLOW_HEAL)
        self.assertEqual(mercy_plan.reason, "gemini_shortcircuit(critical_mercy)")
        fake_client.decisions.create.assert_not_called()

        afk_plan = strat.evaluate_wave(
            make_snapshot(stagnation_seconds=200.0, wave_damage_taken=0, flow_velocity=0.0)
        )
        self.assertEqual(afk_plan.strategy, "gemini_decision")
        self.assertEqual(afk_plan.reason, "gemini_shortcircuit(afk_stagnation)")
        fake_client.decisions.create.assert_not_called()

        finale_decision = strat.evaluate_finale_boss_stage(
            make_snapshot(
                finale_active=True,
                cur_finale_stage=int(FinaleStage.FINALE_FINAL_BOSS),
                down_or_dead_count=2,
                avg_eff_hp=25.0,
            ),
            int(FinaleStage.FINALE_FINAL_BOSS),
        )
        self.assertEqual(finale_decision.strategy, "gemini_decision")
        self.assertEqual(finale_decision.boss_directive, BossDirective.BLOCK_EXTRA_RESPAWN)
        fake_client.decisions.create.assert_not_called()

    def test_dynamic_question_pruning_omits_locked_boss_directive(self) -> None:
        from types import SimpleNamespace

        fake_answers = {
            "diagnosis": SimpleNamespace(type="choice", choice="HEALTHY_STEADY_PUSH"),
            "intent": SimpleNamespace(type="choice", choice="MAINTAIN_GAUNTLET_FLOW"),
            "target_role": SimpleNamespace(type="choice", choice="P1_FRONT"),
            "spawn_dir": SimpleNamespace(type="choice", choice="FRONT"),
            "horde_directive": SimpleNamespace(type="choice", choice="KEEP_NATIVE"),
            "use_freeze_ambush": SimpleNamespace(type="noul", noul=0.7),
            "use_focus_strike": SimpleNamespace(type="noul", noul=0.6),
            "lead_si_class": SimpleNamespace(type="choice", choice="Hunter"),
            "support_si_class": SimpleNamespace(type="choice", choice="Spitter"),
            "aggression_score": SimpleNamespace(type="score", score=3.0),
        }
        fake_client = SimpleNamespace(
            decisions=SimpleNamespace(
                create=mock.MagicMock(return_value=SimpleNamespace(answers=fake_answers))
            )
        )
        strat = GeminiDecisionStrategy(client=fake_client)

        plan_tank = strat.evaluate_wave(make_snapshot(tank_alive=True, avg_eff_hp=50.0))
        self.assertNotIn("boss_directive", fake_client.decisions.create.call_args.kwargs["questions"])
        self.assertEqual(plan_tank.boss_directive, BossDirective.RESPECT_NATIVE)

        fake_client.decisions.create.reset_mock()
        plan_finale = strat.evaluate_wave(
            make_snapshot(
                finale_active=True,
                cur_finale_stage=int(FinaleStage.FINALE_GAUNTLET_1),
                avg_eff_hp=85.0,
            )
        )
        self.assertNotIn("boss_directive", fake_client.decisions.create.call_args.kwargs["questions"])
        self.assertEqual(plan_finale.boss_directive, BossDirective.RESPECT_NATIVE)

    def test_decision_audit_log_records_answers_and_usage(self) -> None:
        from types import SimpleNamespace

        fake_answers = {
            "diagnosis": SimpleNamespace(
                type="choice",
                choice="HEALTHY_STEADY_PUSH",
                confidence=0.91,
                probabilities={"HEALTHY_STEADY_PUSH": 0.91, "CORNER_CAMPING_STRONG": 0.09},
            ),
            "intent": SimpleNamespace(type="choice", choice="ESCALATE_CLIMAX", confidence=0.88),
            "target_role": SimpleNamespace(type="choice", choice="P1_FRONT", confidence=0.85),
            "spawn_dir": SimpleNamespace(type="choice", choice="FRONT", confidence=0.80),
            "horde_directive": SimpleNamespace(type="choice", choice="SCALE_1.6X", confidence=0.87),
            "boss_directive": SimpleNamespace(type="choice", choice="ADD_1_TANK", confidence=0.79),
            "use_freeze_ambush": SimpleNamespace(type="noul", noul=0.90, confidence=0.90),
            "use_focus_strike": SimpleNamespace(type="noul", noul=0.82, confidence=0.82),
            "lead_si_class": SimpleNamespace(type="choice", choice="Charger", confidence=0.84),
            "support_si_class": SimpleNamespace(type="choice", choice="Spitter", confidence=0.81),
            "aggression_score": SimpleNamespace(type="score", score=3.5, confidence=0.86),
        }
        fake_usage = SimpleNamespace(
            total_input_tokens=324,
            input_tokens_by_modality=[{"modality": "text", "tokens": 324}],
            total_output_tokens=0,
            total_tokens=324,
        )
        fake_client = SimpleNamespace(
            decisions=SimpleNamespace(
                create=mock.MagicMock(
                    side_effect=[
                        SimpleNamespace(answers=fake_answers, usage=fake_usage),
                        SimpleNamespace(
                            answers={
                                "boss_directive": SimpleNamespace(
                                    type="choice", choice="ADD_2_TANKS", confidence=0.94
                                )
                            },
                            usage=fake_usage,
                        ),
                    ]
                )
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "gemini_decision_audit.log"
            strat = GeminiDecisionStrategy(client=fake_client, audit_log_path=audit_path)

            strat.evaluate_wave(make_snapshot(eval_seq=11, map_name="c2m1_highway", wave_number=2))
            strat.evaluate_finale_boss_stage(
                make_snapshot(
                    eval_seq=12,
                    finale_active=True,
                    cur_finale_stage=int(FinaleStage.FINALE_FINAL_BOSS),
                ),
                int(FinaleStage.FINALE_FINAL_BOSS),
            )

            self.assertTrue(audit_path.is_file())
            lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 2)

            entries = [json.loads(line.split("] ", 1)[1]) for line in lines]
            first, second = entries

            self.assertEqual(first["call_type"], "EvaluateWave")
            self.assertEqual(first["eval_seq"], 11)
            self.assertEqual(first["usage"]["total_input_tokens"], 324)
            self.assertEqual(
                first["usage"]["input_tokens_by_modality"],
                [{"modality": "text", "tokens": 324}],
            )
            self.assertEqual(first["usage"]["total_output_tokens"], 0)
            self.assertEqual(first["usage"]["total_tokens"], 324)
            self.assertEqual(first["cumulative_usage"], {"calls": 1, "total_input_tokens": 324, "total_tokens": 324})
            self.assertEqual(first["answers"]["diagnosis"]["choice"], "HEALTHY_STEADY_PUSH")
            self.assertAlmostEqual(first["answers"]["diagnosis"]["confidence"], 0.91)
            self.assertEqual(
                first["answers"]["diagnosis"]["probabilities"],
                {"HEALTHY_STEADY_PUSH": 0.91, "CORNER_CAMPING_STRONG": 0.09},
            )

            self.assertEqual(second["call_type"], "EvaluateFinaleBossStage")
            self.assertEqual(second["eval_seq"], 12)
            self.assertEqual(second["cumulative_usage"], {"calls": 2, "total_input_tokens": 648, "total_tokens": 648})
            self.assertEqual(second["answers"]["boss_directive"]["choice"], "ADD_2_TANKS")

            status = strat.get_status()
            self.assertEqual(status["audit_calls"], 2)
            self.assertEqual(status["audit_total_input_tokens"], 648)
            self.assertEqual(status["audit_total_tokens"], 648)

    def test_wave_and_finale_questions_match_sdk_schema(self) -> None:
        from google.genai._gaos.types.decisions import CreateDecisionRequest
        from director.strategies.gemini_decision_strategy import _FINALE_QUESTIONS, _WAVE_QUESTIONS

        for question_set in (_WAVE_QUESTIONS, _FINALE_QUESTIONS):
            req = CreateDecisionRequest.model_validate(
                {"model": "gemini-decision-1", "input": "test", "questions": question_set}
            )
            for qname, qmodel in req.questions.items():
                self.assertFalse(
                    getattr(qmodel, "__pydantic_extra__", None),
                    f"Unexpected extra fields on question {qname}: {getattr(qmodel, '__pydantic_extra__', None)}",
                )
                criteria = getattr(qmodel, "criteria", None)
                self.assertFalse(
                    getattr(criteria, "__pydantic_extra__", None),
                    f"Unexpected extra fields on criteria for {qname}: {getattr(criteria, '__pydantic_extra__', None)}",
                )

        wave_req = CreateDecisionRequest.model_validate(
            {"model": "gemini-decision-1", "input": "test", "questions": _WAVE_QUESTIONS}
        )
        for noul_key in ("use_freeze_ambush", "use_focus_strike"):
            criteria = wave_req.questions[noul_key].criteria
            self.assertTrue(criteria.true_description)
            self.assertTrue(criteria.false_description)


if __name__ == "__main__":
    unittest.main()
