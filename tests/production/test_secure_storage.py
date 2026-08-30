from __future__ import annotations

import os
from pathlib import Path

import pytest
from phone_agent_gateway.ai_bridge.secure_storage import (
    SecureStorageError,
    append_private_line,
    atomic_write_private,
)


def test_private_atomic_write_and_append_enforce_permissions(tmp_path: Path) -> None:
    target = tmp_path / "private" / "data.json"
    atomic_write_private(target, '{"one":1}\n')
    assert target.read_text() == '{"one":1}\n'
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert os.stat(target.parent).st_mode & 0o777 == 0o700

    log = tmp_path / "logs" / "events.log"
    append_private_line(log, "first")
    append_private_line(log, "second")
    assert log.read_text() == "first\nsecond\n"
    assert os.stat(log).st_mode & 0o777 == 0o600


def test_private_write_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("safe")
    private = tmp_path / "private"
    private.mkdir()
    linked = private / "data"
    linked.symlink_to(outside)
    with pytest.raises(SecureStorageError, match="non-symlink"):
        atomic_write_private(linked, "changed")
    assert outside.read_text() == "safe"
