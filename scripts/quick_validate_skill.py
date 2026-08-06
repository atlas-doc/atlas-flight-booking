"""Validate the minimal public structure and frontmatter of a Skill package."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate(skill_directory: Path) -> list[str]:
    errors: list[str] = []
    skill_file = skill_directory / "SKILL.md"
    if not skill_file.is_file():
        return ["SKILL.md is missing"]

    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ["SKILL.md must start with YAML frontmatter"]
    try:
        _, frontmatter, body = text.split("---", 2)
        metadata = yaml.safe_load(frontmatter)
    except (ValueError, yaml.YAMLError):
        return ["SKILL.md frontmatter is invalid YAML"]

    if not isinstance(metadata, dict):
        return ["SKILL.md frontmatter must be a mapping"]
    if set(metadata) != {"name", "description"}:
        errors.append("frontmatter must contain only name and description")

    name = metadata.get("name")
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name) or len(name) > 64:
        errors.append("name must be lowercase hyphen-case and at most 64 characters")
    elif skill_directory.name != name:
        errors.append("Skill directory name must match frontmatter name")

    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("description must be a non-empty string")
    if not body.strip():
        errors.append("SKILL.md body must not be empty")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: quick_validate_skill.py <skill-directory>", file=sys.stderr)
        return 2
    errors = validate(Path(sys.argv[1]))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Skill structure is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
