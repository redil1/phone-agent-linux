"""Asynchronous long-term memory with a private local store and Graphiti mirror."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_MEMORY_DB = Path.home() / ".local" / "share" / "phone-agent" / "identity-memory.sqlite3"
MAX_QUEUE = 2_000


class IdentityMemoryError(RuntimeError):
    pass


def scope_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class MemoryEpisode:
    episode_id: str
    group_id: str
    role: str
    content: str
    language: str
    task_id: str
    reference_time: str


class LocalEpisodeStore:
    def __init__(self, path: Path = DEFAULT_MEMORY_DB) -> None:
        self.path = path.expanduser()
        if not self.path.is_absolute():
            raise IdentityMemoryError("identity memory database path must be absolute")
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_mode = self.path.parent.lstat().st_mode
        if not stat.S_ISDIR(parent_mode) or stat.S_ISLNK(parent_mode):
            raise IdentityMemoryError("identity memory parent must be a real directory")
        if self.path.parent.stat().st_uid != os.getuid() or parent_mode & 0o022:
            raise IdentityMemoryError(
                "identity memory parent must be user-owned and not group/world writable"
            )
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    language TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    reference_time TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_group_time
                    ON episodes(group_id, created_at DESC);
                """
            )
        os.chmod(self.path, 0o600)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def add(self, episode: MemoryEpisode) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode.episode_id,
                    episode.group_id,
                    episode.role,
                    episode.content[:2_000],
                    episode.language,
                    episode.task_id,
                    episode.reference_time,
                    int(time.time()),
                ),
            )
        os.chmod(self.path, 0o600)

    def search(self, group_id: str, query: str, *, limit: int = 5) -> list[dict[str, str]]:
        terms = [
            term.lower()
            for term in re.findall(r"[\wÀ-ÿ]{3,}", query)
            if term.lower() not in {"the", "and", "for", "les", "des", "une", "avec"}
        ][:8]
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT role, content, task_id, reference_time FROM episodes "
                "WHERE group_id=? ORDER BY created_at DESC LIMIT 100",
                (group_id,),
            ).fetchall()
        scored = []
        for role, content, task_id, reference_time in rows:
            lowered = str(content).lower()
            score = sum(term in lowered for term in terms)
            if score or not terms:
                scored.append((score, reference_time, role, content, task_id))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [
            {
                "role": str(role),
                "content": str(content),
                "task_id": str(task_id),
                "reference_time": str(reference_time),
            }
            for _, reference_time, role, content, task_id in scored[:limit]
        ]

    def count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])


class GraphitiHttpMirror:
    """Best-effort mirror to the official Graphiti REST service contract."""

    def __init__(
        self, base_url: str, *, token: str = "", allowed_hosts: set[str] | None = None
    ) -> None:
        parsed = urlparse(base_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        configured_hosts = {
            value.strip().lower()
            for value in os.getenv("PHONE_AGENT_GRAPHITI_ALLOWED_HOSTS", "").split(",")
            if value.strip()
        }
        trusted_hosts = allowed_hosts if allowed_hosts is not None else configured_hosts
        if not (
            (parsed.scheme == "http" and loopback and parsed.port)
            or (
                parsed.scheme == "https"
                and parsed.hostname
                and parsed.hostname.lower() in trusted_hosts
                and not parsed.username
            )
        ):
            raise IdentityMemoryError(
                "Graphiti URL must be explicit loopback HTTP or allowlisted HTTPS"
            )
        if parsed.query or parsed.fragment:
            raise IdentityMemoryError("Graphiti URL must not contain query or fragment")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read(256 * 1024)
        except (OSError, urllib.error.URLError) as exc:
            raise IdentityMemoryError("Graphiti request failed") from exc
        value = json.loads(body.decode()) if body else {}
        if not isinstance(value, dict):
            raise IdentityMemoryError("Graphiti returned an invalid result")
        return value

    def add(self, episode: MemoryEpisode) -> None:
        self._request(
            "/messages",
            {
                "group_id": episode.group_id,
                "messages": [
                    {
                        "uuid": episode.episode_id,
                        "name": episode.episode_id,
                        "role": episode.role,
                        "role_type": episode.role,
                        "content": episode.content,
                        "timestamp": episode.reference_time,
                        "source_description": "PhoneAgent verified conversation episode",
                    }
                ],
            },
        )

    def search(self, group_id: str, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        value = self._request(
            "/search", {"group_ids": [group_id], "query": query, "max_facts": limit}
        )
        facts = value.get("facts")
        return facts if isinstance(facts, list) else []


class AsyncIdentityMemory:
    def __init__(
        self,
        local: LocalEpisodeStore | None = None,
        mirror: GraphitiHttpMirror | None = None,
    ) -> None:
        self.local = local or LocalEpisodeStore(
            Path(os.getenv("PHONE_AGENT_IDENTITY_MEMORY_DB", "").strip() or DEFAULT_MEMORY_DB)
        )
        if mirror is None:
            url = os.getenv("PHONE_AGENT_GRAPHITI_URL", "").strip()
            mirror = (
                GraphitiHttpMirror(url, token=os.getenv("PHONE_AGENT_GRAPHITI_TOKEN", "").strip())
                if url
                else None
            )
        self.mirror = mirror
        self._queue: queue.Queue[MemoryEpisode | None] = queue.Queue(MAX_QUEUE)
        self._dropped = 0
        self._mirrored = 0
        self._last_error = ""
        self._worker = threading.Thread(
            target=self._run, name="identity-memory-worker", daemon=True
        )
        self._worker.start()

    def submit_turn(
        self,
        *,
        caller_id: str,
        call_id: str,
        role: str,
        content: str,
        language: str,
        task_id: str,
    ) -> bool:
        text = str(content).strip()
        if not text or role not in {"caller", "agent"}:
            return False
        group_id = scope_hash(caller_id)
        basis = f"{call_id}:{role}:{text}:{time.time_ns()}"
        episode = MemoryEpisode(
            episode_id=hashlib.sha256(basis.encode()).hexdigest(),
            group_id=group_id,
            role=role,
            content=text[:2_000],
            language=language[:12],
            task_id=task_id[:80],
            reference_time=datetime.now(UTC).isoformat(),
        )
        try:
            self._queue.put_nowait(episode)
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def _run(self) -> None:
        while True:
            episode = self._queue.get()
            if episode is None:
                return
            try:
                self.local.add(episode)
            except Exception as exc:
                self._last_error = f"local:{type(exc).__name__}"
                continue
            if self.mirror is not None:
                try:
                    self.mirror.add(episode)
                    self._mirrored += 1
                except Exception as exc:
                    # The local durable episode is authoritative. A remote mirror
                    # outage never blocks or degrades a live call.
                    self._last_error = f"graphiti:{type(exc).__name__}"

    def search_local(self, caller_id: str, query: str, *, limit: int = 5) -> list[dict[str, str]]:
        return self.local.search(scope_hash(caller_id), query, limit=limit)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "local_episodes": self.local.count(),
            "queue_depth": self._queue.qsize(),
            "queue_capacity": MAX_QUEUE,
            "dropped": self._dropped,
            "graphiti_enabled": self.mirror is not None,
            "graphiti_mirrored": self._mirrored,
            "last_error": self._last_error,
        }


_SERVICE: AsyncIdentityMemory | None = None
_SERVICE_LOCK = threading.Lock()


def get_identity_memory() -> AsyncIdentityMemory:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = AsyncIdentityMemory()
        return _SERVICE
