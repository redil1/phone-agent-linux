#!/usr/bin/env python3
"""Safety-gated live recovery proof for the authenticated Android gateway."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase

from .framed_link import LinkDisconnected, LinkRejected, load_link_key
from .gateway_client import CallState
from .protocol_client import AuthenticatedPhoneAgentClient


@dataclass(slots=True)
class RecoveryResult:
    call_id: str
    initial_epoch: str
    process_recovery_epoch: str
    adb_recovery_epoch: str
    initial_pid: str
    restarted_pid: str
    generation_before: int
    generation_after_flush: int
    generation_after_process_recovery: int
    generation_after_adb_recovery: int
    process_disconnect_detected: bool
    adb_disconnect_detected: bool
    persisted_replay_match: bool
    command_id_binding_rejected: bool
    process_recovery_ms: float
    adb_recovery_ms: float
    final_state: str


def adb(serial: str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["adb", "-s", serial, *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def start_gateway(serial: str) -> None:
    adb(
        serial,
        "shell",
        "am",
        "start-foreground-service",
        "-n",
        "com.phoneagent.gateway/.GatewayService",
    )


def phone_pid(serial: str) -> str:
    return adb(
        serial,
        "shell",
        "pidof",
        "com.phoneagent.gateway",
        check=False,
    ).stdout.strip()


def wait_until(predicate, *, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(message)


def wait_for_authenticated_link(client: AuthenticatedPhoneAgentClient, timeout: float) -> None:
    def ready() -> bool:
        if not client.link.connected:
            return False
        try:
            return client.get_health().get("gateway") == "ready"
        except (LinkDisconnected, LinkRejected):
            return False

    wait_until(ready, timeout=timeout, message="authenticated link did not recover")


def require_idle(client: AuthenticatedPhoneAgentClient) -> None:
    status = client.get_status()
    if status.state not in {CallState.IDLE, CallState.DISCONNECTED}:
        raise RuntimeError(f"refusing recovery mutation while call state is {status.state.value}")


def trigger_disconnect(client: AuthenticatedPhoneAgentClient) -> bool:
    try:
        client.get_health()
    except LinkDisconnected:
        return True
    return False


def parse_generation(client: AuthenticatedPhoneAgentClient) -> int:
    response = client.get_audio_status()
    return int(response["audio"]["generation"])


def run(args: argparse.Namespace) -> RecoveryResult:
    if not args.confirm_process_restart or not args.confirm_adb_restart:
        raise RuntimeError(
            "--confirm-process-restart and --confirm-adb-restart are both required"
        )

    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    client = AuthenticatedPhoneAgentClient(
        session,
        load_link_key(args.key_file),
        device_id=args.device_id,
    )
    process_started = False
    client.link.start_supervisor()
    try:
        wait_for_authenticated_link(client, args.recovery_timeout)
        require_idle(client)
        initial_epoch = str(session.link_epoch)
        initial_pid = phone_pid(args.device_id)
        if not initial_pid:
            raise RuntimeError("gateway process is not running")

        generation_before = parse_generation(client)
        advance = session.interrupt("recovery_probe_generation_floor")
        flush = client.flush_audio(advance)
        generation_after_flush = int(flush["generation"])

        replay_id = uuid4()
        original_replay = client.link.request("gateway.health", command_id=replay_id)

        process_started_at = time.monotonic()
        adb(args.device_id, "shell", "am", "force-stop", "com.phoneagent.gateway")
        wait_until(
            lambda: not phone_pid(args.device_id),
            timeout=5.0,
            message="gateway process did not stop",
        )
        process_disconnect_detected = trigger_disconnect(client)
        start_gateway(args.device_id)
        process_started = True
        wait_for_authenticated_link(client, args.recovery_timeout)
        process_recovery_ms = (time.monotonic() - process_started_at) * 1000
        restarted_pid = phone_pid(args.device_id)
        if not restarted_pid or restarted_pid == initial_pid:
            raise RuntimeError("gateway process PID did not change")
        process_epoch = str(session.link_epoch)
        if process_epoch == initial_epoch:
            raise RuntimeError("process recovery did not establish a new link epoch")

        replayed = client.link.request("gateway.health", command_id=replay_id)
        persisted_replay_match = replayed == original_replay
        if not persisted_replay_match:
            raise RuntimeError("persisted command replay did not return the original result")
        command_id_binding_rejected = False
        try:
            client.link.request("call.status", command_id=replay_id)
        except LinkRejected:
            command_id_binding_rejected = True
        if not command_id_binding_rejected:
            raise RuntimeError("command UUID reuse with a different operation was accepted")

        generation_after_process = parse_generation(client)
        if generation_after_process < generation_after_flush:
            raise RuntimeError("generation moved backwards after Android process recovery")

        adb_replay_id = uuid4()
        original_adb_replay = client.link.request(
            "gateway.health", command_id=adb_replay_id
        )
        adb_epoch_before = str(session.link_epoch)
        adb_started_at = time.monotonic()
        subprocess.run(["adb", "kill-server"], check=True, capture_output=True, text=True)
        adb_disconnect_detected = trigger_disconnect(client)
        wait_for_authenticated_link(client, args.recovery_timeout)
        adb_recovery_ms = (time.monotonic() - adb_started_at) * 1000
        adb_epoch = str(session.link_epoch)
        if adb_epoch == adb_epoch_before:
            raise RuntimeError("ADB recovery did not establish a new link epoch")
        if client.link.request("gateway.health", command_id=adb_replay_id) != original_adb_replay:
            raise RuntimeError("command replay changed across ADB recovery")

        generation_after_adb = parse_generation(client)
        if generation_after_adb < generation_after_process:
            raise RuntimeError("generation moved backwards after ADB recovery")
        require_idle(client)
        final_state = client.get_status().state.value

        result = RecoveryResult(
            call_id=str(session.call_id),
            initial_epoch=initial_epoch,
            process_recovery_epoch=process_epoch,
            adb_recovery_epoch=adb_epoch,
            initial_pid=initial_pid,
            restarted_pid=restarted_pid,
            generation_before=generation_before,
            generation_after_flush=generation_after_flush,
            generation_after_process_recovery=generation_after_process,
            generation_after_adb_recovery=generation_after_adb,
            process_disconnect_detected=process_disconnect_detected,
            adb_disconnect_detected=adb_disconnect_detected,
            persisted_replay_match=persisted_replay_match,
            command_id_binding_rejected=command_id_binding_rejected,
            process_recovery_ms=process_recovery_ms,
            adb_recovery_ms=adb_recovery_ms,
            final_state=final_state,
        )
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n")
        return result
    finally:
        if not process_started and not phone_pid(args.device_id):
            try:
                start_gateway(args.device_id)
            except Exception:
                pass
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", required=True)
    parser.add_argument(
        "--key-file",
        default=str(Path.home() / ".config" / "phone-agent" / "link.key"),
    )
    parser.add_argument(
        "--output",
        default="artifacts/recovery/live-recovery-proof.json",
    )
    parser.add_argument("--recovery-timeout", type=float, default=30.0)
    parser.add_argument("--confirm-process-restart", action="store_true")
    parser.add_argument("--confirm-adb-restart", action="store_true")
    return parser.parse_args()


def main() -> None:
    try:
        result = run(parse_args())
    except Exception as exc:
        print(f"Recovery probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
