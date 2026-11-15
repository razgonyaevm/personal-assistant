from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant.tools import ToolRegistry

IGNORED_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".idea", ".pytest_cache"}


class WorkspaceFS:
    def __init__(self, root_dir: str | Path) -> None:
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve_safe(self, relative_path: str = ".") -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PermissionError(
                f"Доступ запрещён: путь '{relative_path}' выходит за пределы workspace."
            )
        return candidate

    def list_directory(self, path: str = ".", max_depth: int = 3) -> str:
        target = self.resolve_safe(path)
        if not target.exists():
            raise FileNotFoundError(f"Путь не найден: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Это файл, а не директория: {path}")

        lines: list[str] = [f"{target.relative_to(self.root) if target != self.root else '.'}/"]

        def walk(current: Path, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            entries = sorted(
                (e for e in current.iterdir() if e.name not in IGNORED_DIRS),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
            for idx, entry in enumerate(entries):
                is_last = idx == len(entries) - 1
                connector = "└── " if is_last else "├── "
                suffix = "/" if entry.is_dir() else f" ({entry.stat().st_size} B)"
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                if entry.is_dir():
                    extension = "    " if is_last else "│   "
                    if depth < max_depth:
                        walk(entry, prefix + extension, depth + 1)
                    elif any(entry.iterdir()):
                        lines.append(f"{prefix}{extension}└── ...")

        walk(target, "", 1)
        return "\n".join(lines)

    def read_file(
            self,
            path: str,
            start_line: int = 1,
            end_line: int | None = None,
    ) -> str:
        target = self.resolve_safe(path)
        if not target.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")

        raw_lines = target.read_text(encoding="utf-8").splitlines()
        total = len(raw_lines)
        start = max(1, start_line)
        end = min(total, end_line) if end_line is not None else total

        if start > total and total > 0:
            return f"Файл '{path}' содержит всего {total} строк."

        numbered = [
            f"{i:>4} | {raw_lines[i - 1]}"
            for i in range(start, end + 1)
        ]
        header = f"Файл: {path} (строки {start}-{end} из {total})"
        return header + "\n" + "\n".join(numbered)

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = self.resolve_safe(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "status": "ok",
            "path": path,
            "bytes_written": len(content.encode("utf-8")),
        }

    def edit_file(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        target = self.resolve_safe(path)
        if not target.is_file():
            raise FileNotFoundError(f"Файл не найден: {path}")

        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old_text)
        if occurrences == 0:
            raise ValueError(
                f"Фрагмент old_text не найден в файле '{path}'. Проверьте точное совпадение пробелов и строк."
            )
        if occurrences > 1:
            raise ValueError(
                f"Фрагмент old_text найден {occurrences} раз(а) в '{path}'. Передайте более длинный уникальный кусок."
            )

        updated = text.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")
        return {"status": "ok", "path": path, "replaced": 1}


def register_fs_tools(registry: ToolRegistry, fs: WorkspaceFS) -> None:
    registry.register(
        name="list_directory",
        description="Возвращает иерархическое дерево файлов и папок внутри рабочей директории.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Относительный путь к папке (по умолчанию '.').",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Максимальная глубина обхода дерева (по умолчанию 3).",
                },
            },
            "required": [],
        },
        handler=fs.list_directory,
    )

    registry.register(
        name="read_file",
        description="Читает текстовый файл с нумерацией строк.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Относительный путь к файлу."},
                "start_line": {"type": "integer", "description": "Начальная строка (с 1)."},
                "end_line": {"type": "integer", "description": "Конечная строка (включительно)."},
            },
            "required": ["path"],
        },
        handler=fs.read_file,
    )

    registry.register(
        name="write_file",
        description="Создаёт новый файл или полностью перезаписывает существующий.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Относительный путь к файлу."},
                "content": {"type": "string", "description": "Полное содержимое файла."},
            },
            "required": ["path", "content"],
        },
        handler=fs.write_file,
    )

    registry.register(
        name="edit_file",
        description="Точечно заменяет уникальный фрагмент текста old_text на new_text в существующем файле.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Относительный путь к файлу."},
                "old_text": {"type": "string", "description": "Точный существующий фрагмент."},
                "new_text": {"type": "string", "description": "Новый фрагмент на замену."},
            },
            "required": ["path", "old_text", "new_text"],
        },
        handler=fs.edit_file,
    )
