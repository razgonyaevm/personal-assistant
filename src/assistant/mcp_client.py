from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from typing import Any
import httpx

from assistant.tools import ToolRegistry

ENV_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _expand_env(value: str) -> str:
    return ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), value)


def _sanitize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Приводит inputSchema MCP к формату, который гарантированно принимает Gemini/LiteLLM."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            result = {}
            for k, v in node.items():
                if k in ("$schema", "additionalProperties"):
                    continue
                result[k] = clean(v)
            return result
        if isinstance(node, list):
            return [clean(item) for item in node]
        return node

    cleaned = clean(schema)
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


def _extract_mcp_text(result: dict[str, Any]) -> str:
    """Достаёт текстовое содержимое из ответа tools/call."""
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)
    return json.dumps(result, ensure_ascii=False)


@dataclass
class MCPToolSpec:
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]


class StdioMCPClient:
    def __init__(
            self,
            name: str,
            command: str,
            args: list[str] | None = None,
            env: dict[str, str] | None = None,
            timeout: float = 20.0,
    ) -> None:
        self.name = name
        self.timeout = timeout
        self._req_id = 0
        self._lock = threading.Lock()
        self._responses: dict[int, queue.Queue[dict[str, Any]]] = {}

        resolved_cmd = sys.executable if command == "python" else command
        proc_env = os.environ.copy()
        if env:
            for k, v in env.items():
                proc_env[k] = _expand_env(v)

        self._proc = subprocess.Popen(
            [resolved_cmd, *(args or [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=proc_env,
        )

        self._reader_thread = threading.Thread(
            target=self._read_loop,
            daemon=True,
        )
        self._reader_thread.start()

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._responses:
                self._responses[msg_id].put(msg)

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._responses[req_id] = q

        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": method,
                    "params": params or {},
                }
            )
            msg = q.get(timeout=self.timeout)
        finally:
            self._responses.pop(req_id, None)

        if "error" in msg:
            raise RuntimeError(f"MCP '{self.name}' error: {msg['error']}")
        return msg.get("result", {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "personal-assistant", "version": "0.1.0"},
            },
        )
        self.notify("notifications/initialized")

    def list_tools(self) -> list[MCPToolSpec]:
        res = self.request("tools/list", {})
        tools: list[MCPToolSpec] = []
        for t in res.get("tools", []):
            tools.append(
                MCPToolSpec(
                    server_name=self.name,
                    name=t["name"],
                    description=t.get("description", ""),
                    input_schema=_sanitize_schema(t.get("inputSchema")),
                )
            )
        return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        res = self.request("tools/call", {"name": tool_name, "arguments": arguments})
        if res.get("isError"):
            return f"MCP tool error: {_extract_mcp_text(res)}"
        return _extract_mcp_text(res)

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()


class HttpMCPClient:
    """Клиент для удалённых MCP-серверов (Streamable HTTP / JSON + SSE)."""

    def __init__(
            self,
            name: str,
            url: str,
            headers: dict[str, str] | None = None,
            timeout: float = 20.0,
    ) -> None:
        self.name = name
        self.url = url
        self._req_id = 0
        self._session_id: str | None = None
        expanded_headers = {
            k: _expand_env(v) for k, v in (headers or {}).items()
        }
        self._client = httpx.Client(headers=expanded_headers, timeout=timeout)

    def _parse_response(self, resp: httpx.Response) -> dict[str, Any]:
        if "mcp-session-id" in resp.headers:
            self._session_id = resp.headers["mcp-session-id"]
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            last_data: dict[str, Any] = {}
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw:
                        last_data = json.loads(raw)
            return last_data
        return resp.json() if resp.text.strip() else {}

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._req_id += 1
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = self._client.post(
            self.url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": method,
                "params": params or {},
            },
        )
        msg = self._parse_response(resp)
        if "error" in msg:
            raise RuntimeError(f"MCP '{self.name}' error: {msg['error']}")
        return msg.get("result", {})

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "personal-assistant", "version": "0.1.0"},
            },
        )
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        self._client.post(
            self.url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    def list_tools(self) -> list[MCPToolSpec]:
        res = self.request("tools/list", {})
        return [
            MCPToolSpec(
                server_name=self.name,
                name=t["name"],
                description=t.get("description", ""),
                input_schema=_sanitize_schema(t.get("inputSchema")),
            )
            for t in res.get("tools", [])
        ]

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        res = self.request("tools/call", {"name": tool_name, "arguments": arguments})
        if res.get("isError"):
            return f"MCP tool error: {_extract_mcp_text(res)}"
        return _extract_mcp_text(res)

    def close(self) -> None:
        self._client.close()


class MCPManager:
    """Управляет подключениями ко всем MCP-серверам из mcp_servers.json."""

    def __init__(self) -> None:
        self.clients: dict[str, StdioMCPClient | HttpMCPClient] = {}

    def connect_from_config(self, config_path: str | Path, registry: ToolRegistry) -> list[str]:
        path = Path(config_path)
        if not path.exists():
            return []

        raw = json.loads(path.read_text(encoding="utf-8"))
        servers: dict[str, Any] = raw.get("mcpServers", {})
        registered_names: list[str] = []

        for name, cfg in servers.items():
            if not cfg.get("enabled", True):
                continue

            transport = cfg.get("transport", "stdio").lower()
            if transport == "stdio":
                client: StdioMCPClient | HttpMCPClient = StdioMCPClient(
                    name=name,
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env=cfg.get("env"),
                )
            elif transport == "http":
                client = HttpMCPClient(
                    name=name,
                    url=cfg["url"],
                    headers=cfg.get("headers"),
                )
            else:
                raise ValueError(f"Неизвестный MCP transport '{transport}' для '{name}'")

            client.initialize()
            self.clients[name] = client

            for spec in client.list_tools():
                # Префикс mcp_<server>__<tool> защищает от конфликтов имён между серверами
                exposed_name = f"mcp_{name}__{spec.name}"

                # Замыкание с явной привязкой client и tool_name
                def make_handler(c: StdioMCPClient | HttpMCPClient, t_name: str):
                    return lambda **kwargs: c.call_tool(t_name, kwargs)

                registry.register(
                    name=exposed_name,
                    description=f"[MCP:{name}] {spec.description}",
                    parameters=spec.input_schema,
                    handler=make_handler(client, spec.name),
                )
                registered_names.append(exposed_name)

        return registered_names

    def close_all(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
