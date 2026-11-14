"""HMAC-signed JSON-RPC 2.0 client helpers for sending commands from Python -> SourceMod."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("l4d2_director_rpc")
_BASE_SOURCEMOD_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_SM_HOST = "127.0.0.1"
_DEFAULT_SM_PORT = 13337
_DEFAULT_SECRET = "super_super_secret"


def load_sm_rpc_target() -> tuple[str, int, str]:
    """Read SourceMod RPC host/port/secret from configs/rpc.json or cfg/rpc.json."""
    for candidate in (_BASE_SOURCEMOD_DIR / "configs" / "rpc.json", _BASE_SOURCEMOD_DIR / "cfg" / "rpc.json"):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                secret = str(data.get("secret", _DEFAULT_SECRET))
                sm_port = int(data.get("port", _DEFAULT_SM_PORT))
                return _DEFAULT_SM_HOST, sm_port, secret
            except Exception:
                break
    return _DEFAULT_SM_HOST, _DEFAULT_SM_PORT, _DEFAULT_SECRET


def dump_compact(value: Any) -> str:
    """Serialize JSON matching nlohmann::json::dump() used by sm-ext-rpc."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def compute_hmac(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_request(req: dict[str, Any], secret: str) -> str:
    payload = (
        dump_compact(req["jsonrpc"])
        + dump_compact(req["method"])
        + dump_compact(req["params"])
        + dump_compact(req["timestamp"])
        + dump_compact(req.get("id"))
    )
    return compute_hmac(payload, secret)


def sign_reply(reply: dict[str, Any], secret: str) -> str:
    payload = (
        dump_compact(reply["jsonrpc"])
        + dump_compact(reply["id"])
        + dump_compact(reply["result"])
    )
    return compute_hmac(payload, secret)


async def send_rpc_to_sourcemod(
    host: str,
    port: int,
    secret: str,
    method: str,
    params: list[Any],
    expect_reply: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        req: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "timestamp": int(time.time()),
            "id": str(uuid.uuid4()) if expect_reply else None,
        }
        req["sig"] = sign_request(req, secret)
        writer.write((dump_compact(req) + "\n").encode("utf-8"))
        await writer.drain()
        if not expect_reply:
            return None
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(line.decode("utf-8")) if line else None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def build_director_command_payload(
    action: str,
    *,
    zombie_class: int = 3,
    target_survivor: int = 0,
    spawn_dir: int = 0,
    focus_squad: bool = False,
    cooldown: int = 20,
    horde_directive: int = 0,
    boss_directive: int = 0,
    message: str = "",
) -> dict[str, Any]:
    """Always populate every expected key so SourceMod JSON.Get* natives never error."""
    return {
        "action": str(action),
        "zombie_class": int(zombie_class),
        "target_survivor": int(target_survivor),
        "spawn_dir": int(spawn_dir),
        "focus_squad": bool(focus_squad),
        "cooldown": int(cooldown),
        "horde_directive": int(horde_directive),
        "boss_directive": int(boss_directive),
        "message": str(message),
    }


async def send_director_command(
    host: str,
    port: int,
    secret: str,
    action: str,
    **kwargs: Any,
) -> None:
    payload = build_director_command_payload(action, **kwargs)
    await send_rpc_to_sourcemod(
        host,
        port,
        secret,
        "DirectorCommand",
        [payload],
        expect_reply=False,
    )


def schedule_director_command(action: str, **kwargs: Any) -> bool:
    """Fire-and-forget schedule a DirectorCommand on the active asyncio loop if running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    sm_host, sm_port, secret = load_sm_rpc_target()

    async def _runner() -> None:
        try:
            await send_director_command(sm_host, sm_port, secret, action, **kwargs)
        except Exception as exc:
            _LOGGER.warning("Failed to send DirectorCommand '%s': %s", action, exc)

    loop.create_task(_runner())
    return True
