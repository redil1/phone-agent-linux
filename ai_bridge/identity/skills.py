"""Trusted progressive-disclosure skills for the Identity Kernel."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from ..secure_storage import atomic_write_private, harden_private_file
from .models import LanguageCode, StrictModel, utc_now

BUILTIN_SKILLS = Path(__file__).resolve().parent / "builtin_skills"
MAX_SKILL_BYTES = 64 * 1024
MAX_RESOURCE_BYTES = 128 * 1024
SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
TOOL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


class SkillError(RuntimeError):
    pass


class SkillDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    description: str = Field(min_length=20, max_length=1_000)
    version: str = Field(default="1.0.0", pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    instructions: str = Field(min_length=20, max_length=40_000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    mcp_tools: list[str] = Field(default_factory=list, max_length=64)
    task_ids: list[str] = Field(default_factory=list, max_length=64)
    languages: list[LanguageCode] = Field(
        default_factory=lambda: [LanguageCode.EN, LanguageCode.FR], max_length=2
    )
    priority: int = Field(default=50, ge=0, le=100)
    source: str
    trusted: bool
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    resources: dict[str, str] = Field(default_factory=dict)


class SkillDraft(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    description: str = Field(min_length=20, max_length=1_000)
    version: str = Field(default="1.0.0", pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    instructions: str = Field(min_length=20, max_length=40_000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    mcp_tools: list[str] = Field(default_factory=list, max_length=64)
    task_ids: list[str] = Field(default_factory=list, max_length=64)
    languages: list[LanguageCode] = Field(
        default_factory=lambda: [LanguageCode.EN, LanguageCode.FR], max_length=2
    )
    priority: int = Field(default=50, ge=0, le=89)


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise SkillError(f"skill is missing YAML frontmatter: {path}")
    end = text.find("\n---", 4)
    if end < 0:
        raise SkillError(f"skill frontmatter is unterminated: {path}")
    try:
        metadata = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"skill frontmatter is invalid: {path}") from exc
    if not isinstance(metadata, dict):
        raise SkillError(f"skill frontmatter must be an object: {path}")
    body = text[end + 4 :].strip()
    return metadata, body


class SkillRegistry:
    def __init__(self, identity_root: Path) -> None:
        self.identity_root = identity_root
        self.user_root = identity_root / "skills"
        self.trust_path = identity_root / "skills-trust.json"
        if self.user_root.is_symlink():
            raise SkillError("user skills directory must not be a symlink")
        self.user_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.user_root, 0o700)
        self._lock = threading.RLock()

    def _trust(self) -> dict[str, dict[str, str]]:
        if not self.trust_path.exists():
            return {}
        if self.trust_path.is_symlink() or not self.trust_path.is_file():
            raise SkillError("skill trust store must be a regular file")
        harden_private_file(self.trust_path)
        try:
            value = json.loads(self.trust_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillError("skill trust store is invalid") from exc
        if not isinstance(value, dict):
            raise SkillError("skill trust store must be an object")
        return value

    def trust_skill(self, name: str, digest: str, *, actor: str) -> None:
        if not SKILL_ID_RE.fullmatch(name) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise SkillError("skill trust request is invalid")
        with self._lock:
            trust = self._trust()
            trust[name] = {"digest": digest, "actor": actor[:120], "trusted_at": utc_now()}
            atomic_write_private(
                self.trust_path,
                json.dumps(trust, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

    def save_user_skill(self, value: SkillDraft) -> SkillDefinition:
        if (BUILTIN_SKILLS / value.name).exists():
            raise SkillError("user skills cannot replace built-in skills")
        if any(not TOOL_ID_RE.fullmatch(tool) for tool in [*value.allowed_tools, *value.mcp_tools]):
            raise SkillError("skill contains an invalid tool name")
        directory = self.user_root / value.name
        if directory.is_symlink():
            raise SkillError("skill directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        os.chmod(directory, 0o700)
        metadata = {
            "name": value.name,
            "description": value.description,
            "version": value.version,
            "allowed_tools": value.allowed_tools,
            "mcp_tools": value.mcp_tools,
            "task_ids": value.task_ids,
            "languages": [item.value for item in value.languages],
            "priority": value.priority,
        }
        payload = (
            "---\n"
            + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            + "---\n"
            + value.instructions.strip()
            + "\n"
        )
        atomic_write_private(directory / "SKILL.md", payload)
        skill = self._load(directory, builtin=False)
        if skill.trusted:
            # Editing to bytes that happen to match a previously trusted hash
            # is safe; any changed content remains untrusted by digest.
            return skill
        return skill

    @staticmethod
    def _load_resource(path: Path, root: Path) -> str:
        if path.is_symlink() or not path.is_file() or root.resolve() not in path.resolve().parents:
            raise SkillError("skill resource escapes its skill directory")
        if path.stat().st_size > MAX_RESOURCE_BYTES:
            raise SkillError("skill resource exceeds its size bound")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError("skill resources must be UTF-8 text") from exc

    def _load(self, directory: Path, *, builtin: bool) -> SkillDefinition:
        skill_path = directory / "SKILL.md"
        if skill_path.is_symlink() or not skill_path.is_file():
            raise SkillError(f"skill has no regular SKILL.md: {directory.name}")
        if skill_path.stat().st_size > MAX_SKILL_BYTES:
            raise SkillError("skill instructions exceed their size bound")
        text = skill_path.read_text(encoding="utf-8")
        metadata, instructions = _parse_frontmatter(text, skill_path)
        allowed_fields = {
            "name",
            "description",
            "version",
            "allowed_tools",
            "mcp_tools",
            "task_ids",
            "languages",
            "priority",
        }
        extras = set(metadata) - allowed_fields
        if extras:
            raise SkillError(f"skill contains unsupported fields: {sorted(extras)}")
        name = str(metadata.get("name") or "")
        if name != directory.name or not SKILL_ID_RE.fullmatch(name):
            raise SkillError("skill name must match its directory")
        tools = [str(value) for value in metadata.get("allowed_tools") or []]
        mcp_tools = [str(value) for value in metadata.get("mcp_tools") or []]
        if any(not TOOL_ID_RE.fullmatch(value) for value in [*tools, *mcp_tools]):
            raise SkillError("skill contains an invalid tool name")
        resources: dict[str, str] = {}
        resources_root = directory / "references"
        if resources_root.exists():
            if resources_root.is_symlink() or not resources_root.is_dir():
                raise SkillError("skill references must be a real directory")
            for path in sorted(resources_root.rglob("*")):
                if path.is_file():
                    relative = str(path.relative_to(resources_root))
                    if len(resources) >= 32:
                        raise SkillError("skill has too many resources")
                    resources[relative] = self._load_resource(path, directory)
        digest_payload = json.dumps(
            {"instructions": text, "resources": resources},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = "sha256:" + hashlib.sha256(digest_payload.encode()).hexdigest()
        trusted = builtin or self._trust().get(name, {}).get("digest") == digest
        return SkillDefinition(
            name=name,
            description=str(metadata.get("description") or ""),
            version=str(metadata.get("version") or "1.0.0"),
            instructions=instructions,
            allowed_tools=tools,
            mcp_tools=mcp_tools,
            task_ids=[str(value) for value in metadata.get("task_ids") or []],
            languages=metadata.get("languages") or ["en", "fr"],
            priority=int(metadata.get("priority", 50)),
            source="builtin" if builtin else "user",
            trusted=trusted,
            digest=digest,
            resources=resources,
        )

    def discover(self) -> tuple[dict[str, SkillDefinition], dict[str, str]]:
        skills: dict[str, SkillDefinition] = {}
        errors: dict[str, str] = {}
        for root, builtin in ((BUILTIN_SKILLS, True), (self.user_root, False)):
            if not root.is_dir():
                continue
            for directory in sorted(
                path
                for path in root.iterdir()
                if path.is_dir()
                and path.name != "__pycache__"
                and not path.name.startswith(".")
            ):
                try:
                    if directory.is_symlink():
                        raise SkillError("skill directory must not be a symlink")
                    skill = self._load(directory, builtin=builtin)
                    if skill.name in skills:
                        raise SkillError("a user skill cannot override a built-in skill")
                    skills[skill.name] = skill
                except Exception as exc:
                    errors[directory.name] = str(exc)
        return skills, errors

    def active(
        self, enabled: list[str], *, task_id: str, language: LanguageCode
    ) -> list[SkillDefinition]:
        discovered, _ = self.discover()
        output = []
        for name in enabled:
            skill = discovered.get(name)
            if skill is None or not skill.trusted or language not in skill.languages:
                continue
            if skill.task_ids and task_id not in skill.task_ids:
                continue
            output.append(skill)
        return sorted(output, key=lambda item: (-item.priority, item.name))

    @staticmethod
    def catalog(skills: list[SkillDefinition]) -> str:
        if not skills:
            return "- No progressive skills are enabled."
        return "\n".join(f"- {item.name}: {item.description}" for item in skills)

    @staticmethod
    def load_for_model(skill: SkillDefinition) -> dict[str, Any]:
        return {
            "skill": skill.name,
            "version": skill.version,
            "instructions": skill.instructions,
            "resources": skill.resources,
            "allowed_tools": skill.allowed_tools,
            "mcp_tools": skill.mcp_tools,
        }
