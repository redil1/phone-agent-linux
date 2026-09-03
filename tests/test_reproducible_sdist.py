"""Source-distribution normalization must be byte reproducible and fail closed."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from phone_agent_gateway.release.evidence import sha256
from phone_agent_gateway.release.normalize_sdist import (
    SdistNormalizationError,
    normalize_sdist,
)


def _archive(path: Path, *, mtime: int, uid: int = 1001) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        directory = tarfile.TarInfo("package-1.0")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = mtime
        directory.uid = uid
        archive.addfile(directory)
        payload = b"value = 1\n"
        source = tarfile.TarInfo("package-1.0/module.py")
        source.mode = 0o644
        source.mtime = mtime
        source.uid = uid
        source.size = len(payload)
        archive.addfile(source, io.BytesIO(payload))


def test_different_build_metadata_normalizes_to_identical_sdist(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _archive(first, mtime=100, uid=1001)
    _archive(second, mtime=200, uid=2002)
    assert sha256(first) != sha256(second)

    first_hash = normalize_sdist(first, epoch=1_700_000_000)
    second_hash = normalize_sdist(second, epoch=1_700_000_000)

    assert first_hash == second_hash
    assert first.read_bytes() == second.read_bytes()
    with gzip.open(first) as stream:
        assert b"value = 1" in stream.read()


def test_unsafe_or_non_regular_sdist_members_fail_closed(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        link = tarfile.TarInfo("package/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)

    with pytest.raises(SdistNormalizationError, match="unsupported sdist member"):
        normalize_sdist(archive_path, epoch=1_700_000_000)
