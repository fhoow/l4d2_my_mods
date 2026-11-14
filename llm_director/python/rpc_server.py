#!/usr/bin/env python3
"""SRPC (Dreae/sm-ext-rpc) compatible JSON-RPC 2.0 Director server in Python."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Ensure `python/` is importable regardless of working directory
PYTHON_DIR = Path(__file__).resolve().parent
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

for _env_file in (PYTHON_DIR / ".env", PYTHON_DIR.parent / ".env"):
    if _env_file.is_file():
        load_dotenv(_env_file, override=False)

from director.constants import (
    BOSS_NAMES,
    DIAGNOSIS_NAMES,
    DIRECTION_NAMES,
    HORDE_NAMES,
    INTENT_NAMES,
    FinaleStage,
)
from director.models import TelemetrySnapshot, WavePlan
from director.rpc_client import (
    compute_hmac,
    dump_compact,
    send_director_command,
    send_rpc_to_sourcemod,
    sign_reply,
    sign_request,
)
from director.strategies import StrategyRegistry


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 13338
DEFAULT_SM_HOST = "127.0.0.1"
DEFAULT_SM_PORT = 13337
DEFAULT_SECRET = "super_super_secret"
MAX_REQUEST_AGE_SECONDS = 30


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("l4d2_director_rpc")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    log_dir = PYTHON_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "director_python.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


def load_rpc_config() -> tuple[str, int, str, int, str]:
    base_dir = PYTHON_DIR.parent
    for candidate in (base_dir / "configs" / "rpc.json", base_dir / "cfg" / "rpc.json"):
        if candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            secret = data.get("secret", DEFAULT_SECRET)
            sm_port = int(data.get("port", DEFAULT_SM_PORT))
            py_cfg = data.get("servers", {}).get("python", {})
            host = py_cfg.get("address", DEFAULT_HOST)
            port = int(py_cfg.get("port", DEFAULT_PORT))
            return host, port, DEFAULT_SM_HOST, sm_port, secret
    return DEFAULT_HOST, DEFAULT_PORT, DEFAULT_SM_HOST, DEFAULT_SM_PORT, DEFAULT_SECRET


def _scan_json_string_end(text: str, start: int) -> int:
    """Given text[start] == '\"', return the index right after the closing quote."""
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        i += 1
    raise ValueError("Unterminated JSON string")


def extract_top_level_json_value_slices(raw_line: str) -> dict[str, str]:
    """Extract raw JSON value substrings for top-level keys (preserves exact C++ nlohmann float dumps)."""
    n = len(raw_line)
    i = 0
    while i < n and raw_line[i].isspace():
        i += 1
    if i >= n or raw_line[i] != "{":
        return {}
    i += 1

    slices: dict[str, str] = {}
    while i < n:
        while i < n and (raw_line[i].isspace() or raw_line[i] == ","):
            i += 1
        if i >= n or raw_line[i] == "}":
            break
        if raw_line[i] != '"':
            return {}

        key_end = _scan_json_string_end(raw_line, i)
        key = json.loads(raw_line[i:key_end])
        i = key_end

        while i < n and raw_line[i].isspace():
            i += 1
        if i >= n or raw_line[i] != ":":
            return {}
        i += 1
        while i < n and raw_line[i].isspace():
            i += 1
        if i >= n:
            return {}

        val_start = i
        ch = raw_line[i]
        if ch == '"':
            i = _scan_json_string_end(raw_line, i)
        elif ch in "{[":
            depth = 0
            while i < n:
                c = raw_line[i]
                if c == '"':
                    i = _scan_json_string_end(raw_line, i)
                    continue
                if c in "{[":
                    depth += 1
                elif c in "}]":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        else:
            while i < n and raw_line[i] not in ",}] \t\r\n":
                i += 1
        slices[key] = raw_line[val_start:i]

    return slices


def verify_request_signature(raw_line: str, req: dict[str, Any], secret: str) -> bool:
    sig = req.get("sig")
    if not isinstance(sig, str):
        return False

    expected_compact = sign_request(req, secret)
    if hmac.compare_digest(sig, expected_compact):
        return True

    # Fallback for C++ nlohmann::json float serialization nuances
    try:
        slices = extract_top_level_json_value_slices(raw_line)
        if {"jsonrpc", "method", "params", "timestamp"}.issubset(slices.keys()):
            id_slice = slices.get("id", "null")
            raw_payload = (
                slices["jsonrpc"]
                + slices["method"]
                + slices["params"]
                + slices["timestamp"]
                + id_slice
            )
            return hmac.compare_digest(sig, compute_hmac(raw_payload, secret))
    except Exception:
        return False
    return False


class DirectorRpcRuntime:
    """Holds strategy registry, telemetry cache, and handles RPC methods."""

    def __init__(self, default_strategy: str = "offline_sm", logger: logging.Logger | None = None) -> None:
        self.logger = logger or setup_logger()
        self.registry = StrategyRegistry(default_strategy=default_strategy)
        self.started_at = time.time()
        self.eval_count = 0
        self.finale_eval_count = 0
        self.event_count = 0
        self.tick_count = 0
        self.latest_snapshot: TelemetrySnapshot | None = None
        self.last_plan: WavePlan | None = None
        self.last_event: dict[str, Any] | None = None

    def _params_dict(self, params: list[Any] | dict[str, Any]) -> dict[str, Any]:
        if isinstance(params, dict):
            return params
        if isinstance(params, list) and params and isinstance(params[0], dict):
            return params[0]
        return {}

    def handle_method(self, method: str, params: list[Any] | dict[str, Any]) -> Any:
        if method == "EvaluateWave":
            snapshot = TelemetrySnapshot.from_rpc_params(params)
            self.latest_snapshot = snapshot
            self.eval_count += 1
            plan = self.registry.active.evaluate_wave(snapshot)
            self.last_plan = plan
            res = plan.to_rpc_dict()
            self.logger.info(
                "EvaluateWave #%d [seq=%d map=%s W#%d strat=%s]: skip=%s Diag=%s Intent=%s Dir=%s Count=%d Queue=%s Horde=%s Boss=%s CD=%ds (%s)",
                self.eval_count,
                snapshot.eval_seq,
                snapshot.map_name,
                snapshot.wave_number + 1,
                res["strategy"],
                res["skip_wave"],
                DIAGNOSIS_NAMES[res["diagnosis"]],
                INTENT_NAMES[res["intent"]],
                DIRECTION_NAMES[res["spawn_dir"]],
                res["wave_target_count"],
                res["wave_queue"],
                HORDE_NAMES[res["horde_directive"]],
                BOSS_NAMES[res["boss_directive"]],
                res["cooldown"],
                res["reason"],
            )
            return res

        if method == "EvaluateFinaleBossStage":
            data = self._params_dict(params)
            snapshot = TelemetrySnapshot.from_rpc_params(data)
            self.latest_snapshot = snapshot
            self.finale_eval_count += 1
            finale_type = int(data.get("finale_type", FinaleStage.FINALE_NONE))
            finale_arg = str(data.get("finale_arg", ""))
            decision = self.registry.active.evaluate_finale_boss_stage(snapshot, finale_type, finale_arg)
            res = decision.to_rpc_dict()
            self.logger.info(
                "EvaluateFinaleBossStage #%d [seq=%d stage=%d strat=%s]: Boss=%s (%s)",
                self.finale_eval_count,
                snapshot.eval_seq,
                finale_type,
                res["strategy"],
                BOSS_NAMES[res["boss_directive"]],
                res["reason"],
            )
            return res

        if method == "OnDirectorEvent":
            data = self._params_dict(params)
            event_name = str(data.get("event", data.get("event_name", "unknown")))
            self.event_count += 1
            self.last_event = {"event": event_name, "data": data, "time": time.time()}
            self.registry.active.on_event(event_name, data)
            self.logger.info(
                "Event [%s] map=%s state=%s wave=%s details=%s",
                event_name,
                data.get("map_name", ""),
                data.get("director_state", ""),
                data.get("wave_number", ""),
                data.get("details", ""),
            )
            return {"ok": True, "event": event_name}

        if method == "OnHeartbeatTick":
            snapshot = TelemetrySnapshot.from_rpc_params(params)
            self.latest_snapshot = snapshot
            self.tick_count += 1
            self.registry.active.on_tick(snapshot)
            if self.tick_count % 15 == 1:
                self.logger.info(
                    "Heartbeat #%d map=%s state=%s W#%d alive=%d down=%d avgHP=%.1f flow=%.1f%% stag=%.0fs",
                    self.tick_count,
                    snapshot.map_name,
                    snapshot.director_state,
                    snapshot.wave_number,
                    snapshot.alive_count,
                    snapshot.down_or_dead_count,
                    snapshot.avg_eff_hp,
                    snapshot.peak_flow_ratio,
                    snapshot.stagnation_seconds,
                )
            return {"ok": True, "tick": self.tick_count}

        if method == "SetStrategy":
            if isinstance(params, list) and params:
                target_name = str(params[0])
            else:
                target_name = str(self._params_dict(params).get("strategy", ""))
            try:
                strat = self.registry.set_active(target_name)
                msg = f"Switched active strategy to '{strat.name}'"
                self.logger.info(msg)
                return {
                    "ok": True,
                    "strategy": strat.name,
                    "available": ",".join(self.registry.list_names()),
                    "message": msg,
                }
            except KeyError as exc:
                msg = str(exc)
                return {
                    "ok": False,
                    "strategy": self.registry.active_name,
                    "available": ",".join(self.registry.list_names()),
                    "message": msg,
                }

        if method == "ReloadStrategies":
            names = self.registry.reload()
            msg = f"Reloaded strategies; active='{self.registry.active_name}', available={','.join(names)}"
            self.logger.info(msg)
            return {
                "ok": True,
                "strategy": self.registry.active_name,
                "available": ",".join(names),
                "message": msg,
            }

        if method == "GetStatus":
            snap = self.latest_snapshot
            return {
                "ok": True,
                "active_strategy": self.registry.active_name,
                "strategy_info": self.registry.active.get_status(),
                "available_strategies": self.registry.list_names(),
                "uptime_sec": round(time.time() - self.started_at, 1),
                "eval_count": self.eval_count,
                "finale_eval_count": self.finale_eval_count,
                "event_count": self.event_count,
                "tick_count": self.tick_count,
                "latest_map": snap.map_name if snap else "",
                "latest_wave": snap.wave_number if snap else 0,
            }

        if method == "Echo":
            if isinstance(params, list):
                if len(params) >= 3:
                    userid, player_name, message = params[0], params[1], params[2]
                elif len(params) == 1:
                    userid, player_name, message = 0, "Unknown", params[0]
                else:
                    userid, player_name, message = 0, "Unknown", str(params)
            else:
                userid = params.get("userid", 0)
                player_name = params.get("player_name", "Unknown")
                message = params.get("message", str(params))

            now = time.strftime("%H:%M:%S")
            self.logger.info("Echo from %s (userid=%s): %s", player_name, userid, message)
            return {
                "reply": f"Python RPC 已收到 [{player_name}] 的訊息: {message} ({now})",
                "userid": userid,
                "server_time": now,
                "ok": True,
            }

        if method == "Ping":
            now = time.strftime("%H:%M:%S")
            self.logger.info("Ping received: %s", params)
            return {
                "pong": True,
                "server_time": now,
                "strategy": self.registry.active_name,
            }

        raise KeyError(f"Method not found: {method}")


_DEFAULT_RUNTIME: DirectorRpcRuntime | None = None


def get_default_runtime() -> DirectorRpcRuntime:
    global _DEFAULT_RUNTIME
    if _DEFAULT_RUNTIME is None:
        _DEFAULT_RUNTIME = DirectorRpcRuntime()
    return _DEFAULT_RUNTIME


def handle_method(method: str, params: list[Any] | dict[str, Any]) -> Any:
    return get_default_runtime().handle_method(method, params)


def make_error_response(code: int, message: str, req_id: Any = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": req_id,
    }


def process_raw_request(
    raw_line: str,
    secret: str,
    runtime: DirectorRpcRuntime | None = None,
) -> dict[str, Any] | None:
    rt = runtime or get_default_runtime()
    try:
        req = json.loads(raw_line)
    except json.JSONDecodeError:
        return make_error_response(-32700, "Parse error")

    if not isinstance(req, dict):
        return make_error_response(-32600, "Invalid Request")

    req_id = req.get("id")
    is_notification = not isinstance(req_id, (int, str))

    ts = req.get("timestamp")
    if not isinstance(ts, int) or ts < int(time.time()) - MAX_REQUEST_AGE_SECONDS:
        return make_error_response(-32600, "Invalid or expired timestamp", req_id)

    if not verify_request_signature(raw_line, req, secret):
        return make_error_response(-32600, "Invalid signature", req_id)

    method = req.get("method")
    params = req.get("params")
    if not isinstance(method, str) or not isinstance(params, (list, dict)):
        return make_error_response(-32600, "Invalid Request", req_id)

    try:
        result = rt.handle_method(method, params)
    except KeyError as exc:
        if is_notification:
            return None
        return make_error_response(-32601, str(exc), req_id)
    except Exception as exc:
        rt.logger.exception("RPC method %s failed", method)
        if is_notification:
            return None
        return make_error_response(-32603, f"Internal error: {exc}", req_id)

    if is_notification:
        return None

    reply = {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    }
    reply["sig"] = sign_reply(reply, secret)
    return reply


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    secret: str,
    runtime: DirectorRpcRuntime,
) -> None:
    peer = writer.get_extra_info("peername")
    runtime.logger.info("[RPC] Connected: %s", peer)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            resp = await asyncio.to_thread(process_raw_request, raw, secret, runtime)
            if resp is not None:
                payload = (dump_compact(resp) + "\n").encode("utf-8")
                writer.write(payload)
                await writer.drain()
    except ConnectionResetError:
        pass
    finally:
        runtime.logger.info("[RPC] Disconnected: %s", peer)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_server(
    host: str,
    port: int,
    secret: str,
    runtime: DirectorRpcRuntime,
) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, secret, runtime),
        host,
        port,
        reuse_address=True,
    )
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    runtime.logger.info(
        "[RPC] Python AI Director Server listening on %s (active_strategy=%s, available=%s)",
        addrs,
        runtime.registry.active_name,
        runtime.registry.list_names(),
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    cfg_host, cfg_port, sm_host, sm_port, secret = load_rpc_config()

    parser = argparse.ArgumentParser(description="Python AI Director SRPC server for sm-ext-rpc")
    parser.add_argument("--host", default=cfg_host)
    parser.add_argument("--port", type=int, default=cfg_port)
    parser.add_argument("--strategy", default="offline_sm", help="Initial Director strategy (e.g. offline_sm, llm_hybrid, gemini_decision)")
    parser.add_argument("--send-chat", help="Send a GameChat RPC notification to the L4D2 server and exit")
    parser.add_argument("--send-cmd", help="Send a DirectorCommand action to the L4D2 server (e.g. force_wave, spawn_si)")
    parser.add_argument("--zc", type=int, default=3, help="Zombie class (1..6) for --send-cmd spawn_si")
    parser.add_argument("--target", type=int, default=0, help="Target survivor client index for --send-cmd")
    parser.add_argument("--dir", type=int, default=0, help="Spawn direction (0..4) for --send-cmd")
    parser.add_argument("--cooldown", type=int, default=20, help="Cooldown seconds for --send-cmd set_cooldown")
    args = parser.parse_args()

    if args.send_chat:
        asyncio.run(send_rpc_to_sourcemod(sm_host, sm_port, secret, "GameChat", [args.send_chat]))
        print(f"[RPC] Sent GameChat to {sm_host}:{sm_port}: {args.send_chat}", flush=True)
        return

    if args.send_cmd:
        asyncio.run(
            send_director_command(
                sm_host,
                sm_port,
                secret,
                args.send_cmd,
                zombie_class=args.zc,
                target_survivor=args.target,
                spawn_dir=args.dir,
                cooldown=args.cooldown,
            )
        )
        print(f"[RPC] Sent DirectorCommand '{args.send_cmd}' to {sm_host}:{sm_port}", flush=True)
        return

    logger = setup_logger()
    runtime = DirectorRpcRuntime(default_strategy=args.strategy, logger=logger)
    try:
        asyncio.run(run_server(args.host, args.port, secret, runtime))
    except KeyboardInterrupt:
        logger.info("[RPC] Stopped.")


if __name__ == "__main__":
    main()
