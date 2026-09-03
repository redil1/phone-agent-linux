"""Repack Python source distributions with deterministic, safe tar metadata."""

from __future__ import annotations

import argparse
import gzip
import io
import os
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


class SdistNormalizationError(ValueError):
    """The input archive cannot be normalized without changing its meaning."""


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise SdistNormalizationError(f"unsafe sdist member: {name}")
    return path.as_posix()


def normalize_sdist(path: Path, *, epoch: int) -> str:
    """Normalize one ``.tar.gz`` in place and return its SHA-256 digest."""

    if epoch < 0:
        raise SdistNormalizationError("SOURCE_DATE_EPOCH must be non-negative")
    source = path.resolve()
    if not source.is_file() or source.is_symlink() or not source.name.endswith(".tar.gz"):
        raise SdistNormalizationError("sdist must be a regular .tar.gz file")

    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    try:
        with tarfile.open(source, mode="r:gz") as archive:
            for member in archive.getmembers():
                name = _safe_name(member.name)
                if member.isdir():
                    data = None
                elif member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise SdistNormalizationError(f"could not read sdist member: {name}")
                    data = stream.read()
                else:
                    raise SdistNormalizationError(
                        f"unsupported sdist member type: {name}"
                    )
                normalized = tarfile.TarInfo(name=name)
                normalized.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
                normalized.mode = member.mode
                normalized.uid = 0
                normalized.gid = 0
                normalized.uname = ""
                normalized.gname = ""
                normalized.mtime = epoch
                normalized.size = len(data) if data is not None else 0
                normalized.pax_headers = {}
                members.append((normalized, data))
    except (OSError, tarfile.TarError) as exc:
        raise SdistNormalizationError(f"invalid sdist: {source}") from exc

    members.sort(key=lambda item: item[0].name)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=source.parent, delete=False) as raw:
            temporary = Path(raw.name)
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as out:
                    for member, data in members:
                        out.addfile(member, io.BytesIO(data) if data is not None else None)
        os.replace(temporary, source)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    from .evidence import sha256

    return sha256(source)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sdists", nargs="+", type=Path)
    parser.add_argument(
        "--epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", "1700000000")),
    )
    args = parser.parse_args()
    for sdist in args.sdists:
        print(f"{normalize_sdist(sdist, epoch=args.epoch)}  {sdist}")


if __name__ == "__main__":
    main()
