from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Any

from assistant.tools import ToolRegistry

MAX_OUTPUT_CHARS = 8_000


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [обрезано, всего символов: {len(text)}]"


class DockerSandbox:
    def __init__(self, container_name: str = "assistant-sandbox") -> None:
        self.container_name = container_name

    def _exec(
            self,
            cmd: list[str],
            stdin_text: str | None = None,
            timeout: int = 30,
    ) -> dict[str, Any]:
        docker_cmd = ["docker", "exec", "-i", self.container_name, *cmd]
        try:
            proc = subprocess.run(
                docker_cmd,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": _truncate(proc.stdout.strip()),
                "stderr": _truncate(proc.stderr.strip()),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "exit_code": -1,
                "stdout": _truncate((exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""),
                "stderr": f"Команда превысила таймаут ({timeout} сек).",
                "timed_out": True,
            }

    def run_shell(
            self,
            command: str,
            shell: str = "bash",
            timeout: int = 30,
    ) -> dict[str, Any]:
        shell_norm = shell.lower().strip()
        if shell_norm in ("bash", "linux", "sh"):
            cmd = ["bash", "-lc", command]
            used_shell = "bash"
        elif shell_norm in ("pwsh", "powershell", "powershell7"):
            cmd = ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
            used_shell = "pwsh"
        else:
            return {
                "error": f"Неподдерживаемая оболочка '{shell}'. Используйте 'bash' или 'pwsh'."
            }

        res = self._exec(cmd, timeout=timeout)
        res["shell"] = used_shell
        res["sandbox"] = self.container_name
        return res

    def run_python(self, code: str, timeout: int = 30) -> dict[str, Any]:
        res = self._exec(["python3", "-"], stdin_text=code, timeout=timeout)
        res["runtime"] = "python3"
        res["sandbox"] = self.container_name
        return res

    def get_system_info(self) -> dict[str, Any]:
        host_shells = [s for s in ("zsh", "bash", "pwsh") if shutil.which(s)]
        sandbox_probe = self._exec(
            [
                "bash",
                "-lc",
                "uname -srm && python3 --version && pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'",
            ],
            timeout=10,
        )
        return {
            "host_os": platform.system(),
            "host_release": platform.release(),
            "host_arch": platform.machine(),
            "host_available_shells": host_shells,
            "execution_mode": "docker_sandbox",
            "sandbox_container": self.container_name,
            "sandbox_info": sandbox_probe,
        }


def register_sandbox_tools(registry: ToolRegistry, sandbox: DockerSandbox) -> None:
    registry.register(
        name="run_shell",
        description=(
            "Выполняет shell-команду внутри изолированной Docker-песочницы (рабочая папка /workspace). "
            "Поддерживает Linux 'bash' и PowerShell 7 'pwsh'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Текст команды или скрипта."},
                "shell": {
                    "type": "string",
                    "enum": ["bash", "pwsh"],
                    "description": "Оболочка: 'bash' (Linux) или 'pwsh' (PowerShell 7+).",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Таймаут выполнения в секундах (по умолчанию 30).",
                },
            },
            "required": ["command"],
        },
        handler=sandbox.run_shell,
    )

    registry.register(
        name="run_python",
        description="Выполняет Python-код в изолированной Docker-песочнице внутри /workspace.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Исходный код на Python."},
                "timeout": {"type": "integer", "description": "Таймаут в секундах (по умолчанию 30)."},
            },
            "required": ["code"],
        },
        handler=sandbox.run_python,
    )

    registry.register(
        name="get_system_info",
        description="Возвращает информацию об ОС хоста (macOS/Linux/Windows) и среде песочницы.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=sandbox.get_system_info,
    )
