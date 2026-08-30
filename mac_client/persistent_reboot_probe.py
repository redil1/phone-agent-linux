#!/usr/bin/env python3
"""Prove the persistent system gateway survives reboot without phone repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from phone_agent_gateway.ai_bridge.session import CallSessionState, SessionPhase

from .framed_link import load_link_key
from .gateway_client import CallState
from .protocol_client import AuthenticatedPhoneAgentClient

PACKAGE = "com.phoneagent.gateway"
SYSTEM_APK = "package:/system/priv-app/PhoneAgentGateway/PhoneAgentGateway.apk"
PRIVILEGED_PERMISSIONS = (
    "CAPTURE_AUDIO_OUTPUT",
    "MODIFY_AUDIO_ROUTING",
    "MODIFY_PHONE_STATE",
    "CONTROL_INCALL_EXPERIENCE",
)


@dataclass(slots=True)
class PersistentRebootResult:
    pre_reboot_package_path: str
    post_reboot_package_path: str
    boot_seconds: float
    post_boot_gateway_seconds: float
    service_auto_started: bool
    listeners: list[int]
    privileged_grants: list[str]
    dialer_role: bool
    link_key_hash_match: bool
    persisted_replay_match: bool
    generation_before: int
    generation_after: int
    final_state: str


def adb(serial: str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["adb", "-s", serial, *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def shell(serial: str, command: str, *, check: bool = True) -> str:
    return adb(serial, "shell", command, check=check).stdout.replace("\r", "").strip()


def package_path(serial: str) -> str:
    return shell(serial, f"pm path {PACKAGE}")


def wait_for_boot(serial: str, timeout: float) -> float:
    started = time.monotonic()
    adb(serial, "wait-for-device")
    deadline = started + timeout
    while time.monotonic() < deadline:
        if shell(serial, "getprop sys.boot_completed", check=False) == "1":
            return time.monotonic() - started
        time.sleep(1.0)
    raise TimeoutError("Android did not complete boot")


def listener_ports(serial: str) -> list[int]:
    sockets = shell(serial, "su -c 'ss -lntp'", check=False)
    return [port for port in range(8765, 8769) if f":{port}" in sockets]


def wait_for_gateway_auto_start(serial: str, timeout: float) -> tuple[float, list[int]]:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        service_dump = shell(
            serial,
            f"dumpsys activity services {PACKAGE}",
            check=False,
        )
        listeners = listener_ports(serial)
        if "GatewayService" in service_dump and listeners == [8765, 8766, 8767, 8768]:
            return time.monotonic() - started, listeners
        time.sleep(0.25)
    raise TimeoutError("BootReceiver did not restore GatewayService and all listeners")


def verify_privileged_grants(package_dump: str) -> list[str]:
    granted = [
        permission
        for permission in PRIVILEGED_PERMISSIONS
        if f"android.permission.{permission}: granted=true" in package_dump
    ]
    if len(granted) != len(PRIVILEGED_PERMISSIONS):
        missing = sorted(set(PRIVILEGED_PERMISSIONS) - set(granted))
        raise RuntimeError(f"missing privileged grants after reboot: {missing}")
    return granted


def run(args: argparse.Namespace) -> PersistentRebootResult:
    if not args.confirm_reboot:
        raise RuntimeError("--confirm-reboot is required")
    key = load_link_key(args.key_file)
    session = CallSessionState()
    session.set_phase(SessionPhase.CONNECTING)
    client = AuthenticatedPhoneAgentClient(session, key, device_id=args.device_id)
    try:
        client.connect_control()
        status = client.get_status()
        if status.state not in {CallState.IDLE, CallState.DISCONNECTED}:
            raise RuntimeError(f"refusing reboot while call state is {status.state.value}")
        pre_path = package_path(args.device_id)
        if pre_path != SYSTEM_APK:
            raise RuntimeError(f"pre-reboot package is not the system APK: {pre_path}")

        replay_id = uuid4()
        replay_before = client.link.request("gateway.health", command_id=replay_id)
        generation_before = int(replay_before["generation"])

        # Remove only host-side forwards. No phone service, role, permission, or
        # package command is issued after reboot before the proof checks below.
        adb(args.device_id, "forward", "--remove-all")
        adb(args.device_id, "reboot")
        boot_seconds = wait_for_boot(args.device_id, args.boot_timeout)

        post_path = package_path(args.device_id)
        if post_path != SYSTEM_APK:
            raise RuntimeError(f"post-reboot package is not the system APK: {post_path}")
        package_dump = shell(args.device_id, f"dumpsys package {PACKAGE}")
        grants = verify_privileged_grants(package_dump)
        role = shell(
            args.device_id,
            "cmd role get-role-holders android.app.role.DIALER 0",
        )
        dialer_role = PACKAGE in role.splitlines()
        if not dialer_role:
            raise RuntimeError("PhoneAgent lost ROLE_DIALER after reboot")

        post_boot_gateway_seconds, listeners = wait_for_gateway_auto_start(
            args.device_id,
            args.gateway_timeout,
        )
        service_auto_started = True

        remote_hash = shell(
            args.device_id,
            f"su -c 'sha256sum /data/user/0/{PACKAGE}/files/link.key'",
        ).split()[0]
        local_hash = hashlib.sha256(key).hexdigest()
        key_match = remote_hash == local_hash
        if not key_match:
            raise RuntimeError("phone/Mac link-key hash changed after reboot")

        client.link.reconnect(timeout=5.0)
        replay_after = client.link.request("gateway.health", command_id=replay_id)
        replay_match = replay_after == replay_before
        if not replay_match:
            raise RuntimeError("persisted replay result changed across full reboot")
        current_health = client.get_health()
        generation_after = int(current_health["generation"])
        if generation_after < generation_before:
            raise RuntimeError("generation moved backwards after full reboot")
        final_state = client.get_status().state.value
        if final_state != CallState.IDLE.value:
            raise RuntimeError(f"unexpected post-reboot call state: {final_state}")

        result = PersistentRebootResult(
            pre_reboot_package_path=pre_path,
            post_reboot_package_path=post_path,
            boot_seconds=boot_seconds,
            post_boot_gateway_seconds=post_boot_gateway_seconds,
            service_auto_started=service_auto_started,
            listeners=listeners,
            privileged_grants=grants,
            dialer_role=dialer_role,
            link_key_hash_match=key_match,
            persisted_replay_match=replay_match,
            generation_before=generation_before,
            generation_after=generation_after,
            final_state=final_state,
        )
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n")
        return result
    finally:
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
        default="artifacts/persistent-gsi/persistent-reboot-proof.json",
    )
    parser.add_argument("--boot-timeout", type=float, default=300.0)
    parser.add_argument("--gateway-timeout", type=float, default=30.0)
    parser.add_argument("--confirm-reboot", action="store_true")
    return parser.parse_args()


def main() -> None:
    try:
        result = run(parse_args())
    except Exception as exc:
        print(f"Persistent reboot probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
