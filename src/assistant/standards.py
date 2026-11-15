from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from assistant.tools import ToolRegistry


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    body: str


def load_agents_instructions(workspace_root: Path) -> str:
    """
    Иерархически находит все AGENTS.md в workspace (от корня вглубь)
    и объединяет их в блок инструкций для system prompt.
    """
    files = sorted(
        workspace_root.rglob("AGENTS.md"),
        key=lambda p: (len(p.relative_to(workspace_root).parts), str(p)),
    )
    if not files:
        return ""

    sections: list[str] = []
    for f in files:
        rel = f.relative_to(workspace_root)
        content = f.read_text(encoding="utf-8").strip()
        if content:
            sections.append(f"### Инструкции из `{rel}`\n{content}")

    return "\n\n".join(sections)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text

    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip().strip('"').strip("'")

    return meta, parts[2].strip()


def discover_skills(skills_dir: Path) -> dict[str, SkillMeta]:
    """Сканирует папки вида skills/<skill_name>/SKILL.md."""
    skills: dict[str, SkillMeta] = {}
    if not skills_dir.exists():
        return skills

    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        raw = skill_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name") or skill_file.parent.name
        description = meta.get("description") or "Описание отсутствует."
        skills[name] = SkillMeta(
            name=name,
            description=description,
            path=skill_file,
            body=body,
        )
    return skills


def build_standards_prompt(workspace_root: Path, skills: dict[str, SkillMeta]) -> str:
    parts: list[str] = []

    agents_text = load_agents_instructions(workspace_root)
    if agents_text:
        parts.append("## Правила проекта (AGENTS.md)\n" + agents_text)

    if skills:
        catalog = "\n".join(
            f"- **{s.name}**: {s.description}" for s in skills.values()
        )
        parts.append(
            "## Доступные навыки (SKILL.md)\n"
            "Если задача подходит под один из навыков ниже, сначала вызови инструмент `load_skill`, "
            "чтобы прочитать его полную инструкцию:\n" + catalog
        )

    return "\n\n".join(parts)


def register_skill_tool(registry: ToolRegistry, skills: dict[str, SkillMeta]) -> None:
    def load_skill(name: str) -> str:
        skill = skills.get(name)
        if not skill:
            available = ", ".join(skills.keys()) or "нет"
            return f"Навык '{name}' не найден. Доступные: {available}"
        return f"# Навык: {skill.name}\n\n{skill.body}"

    registry.register(
        name="load_skill",
        description="Загружает полную инструкцию навыка из SKILL.md по его имени.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Имя навыка из списка доступных."}
            },
            "required": ["name"],
        },
        handler=load_skill,
    )
