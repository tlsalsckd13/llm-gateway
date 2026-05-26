from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any


MAX_ZIP_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
BODY_EXCERPT_CHARS = 4096
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class SkillBundleError(ValueError):
    pass


@dataclass(frozen=True)
class SkillBundle:
    frontmatter: dict[str, Any]
    body_excerpt: str
    sha256: str
    files: list[str]
    skill_md_path: str

    @property
    def slug(self) -> str:
        return str(self.frontmatter["name"])

    @property
    def version(self) -> str:
        return str(self.frontmatter["version"])


def validate_and_extract(zip_bytes: bytes) -> SkillBundle:
    return inspect_skill_bundle(zip_bytes)


def inspect_skill_bundle(zip_bytes: bytes) -> SkillBundle:
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise SkillBundleError("skill bundle zip must be 5MB or smaller")

    sha256 = hashlib.sha256(zip_bytes).hexdigest()
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = [info.filename for info in infos]
            _validate_zip_paths(names)
            total_uncompressed = sum(info.file_size for info in infos)
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise SkillBundleError("skill bundle uncompressed size must be 20MB or smaller")

            skill_md_infos = [
                info for info in infos
                if PurePosixPath(info.filename).name.lower() == "skill.md"
            ]
            if len(skill_md_infos) != 1:
                raise SkillBundleError("skill bundle must contain exactly one SKILL.md")

            skill_md_info = skill_md_infos[0]
            raw = archive.read(skill_md_info)
    except zipfile.BadZipFile as exc:
        raise SkillBundleError("skill bundle must be a valid zip file") from exc

    frontmatter, body = parse_skill_markdown(raw.decode("utf-8"))
    _validate_frontmatter(frontmatter)
    return SkillBundle(
        frontmatter=frontmatter,
        body_excerpt=body[:BODY_EXCERPT_CHARS],
        sha256=sha256,
        files=names,
        skill_md_path=skill_md_info.filename,
    )


def parse_skill_markdown(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillBundleError("SKILL.md must start with YAML frontmatter")

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        raise SkillBundleError("SKILL.md frontmatter must be closed with ---")

    frontmatter = _parse_simple_yaml(lines[1:end_index])
    body = "\n".join(lines[end_index + 1:]).strip()
    return frontmatter, body


def _parse_simple_yaml(lines: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    current_list_key: str | None = None

    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if current_list_key and line.startswith(("  - ", "- ")):
            parsed[current_list_key].append(_clean_scalar(line.split("-", 1)[1].strip()))
            continue

        current_list_key = None
        if ":" not in line:
            raise SkillBundleError(f"unsupported frontmatter line: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise SkillBundleError("frontmatter keys must not be empty")
        if value == "":
            parsed[key] = []
            current_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            parsed[key] = [
                _clean_scalar(item.strip())
                for item in value[1:-1].split(",")
                if item.strip()
            ]
        else:
            parsed[key] = _clean_scalar(value)

    return parsed


def _clean_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _validate_zip_paths(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise SkillBundleError(f"unsafe path in skill bundle: {name}")


def _validate_frontmatter(frontmatter: dict[str, Any]) -> None:
    for key in ("name", "description", "version"):
        if not frontmatter.get(key):
            raise SkillBundleError(f"frontmatter requires {key}")

    name = str(frontmatter["name"])
    description = str(frontmatter["description"])
    version = str(frontmatter["version"])

    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillBundleError("frontmatter name must be kebab-case and 64 chars or less")
    if len(description) > 1024:
        raise SkillBundleError("frontmatter description must be 1024 chars or less")
    if not SEMVER_RE.fullmatch(version):
        raise SkillBundleError("frontmatter version must be semver, e.g. 1.0.0")

    for list_key in ("allowed_tools", "tags"):
        if list_key in frontmatter and not isinstance(frontmatter[list_key], list):
            raise SkillBundleError(f"frontmatter {list_key} must be a list")
