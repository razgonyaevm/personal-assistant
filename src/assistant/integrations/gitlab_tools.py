from __future__ import annotations

import base64
import os
from typing import Any
import urllib.parse
from dotenv import load_dotenv
import httpx

from assistant.tools import ToolRegistry

load_dotenv()


class GitLabClient:
    def __init__(
            self,
            base_url: str | None = None,
            token: str | None = None,
            ssl_verify: bool | None = None,
    ) -> None:
        raw_url = base_url or os.getenv("GITLAB_URL", "")
        self.host_url = raw_url.rstrip("/")
        self.api_url = f"{self.host_url}/api/v4" if self.host_url else ""
        self.token = token if token is not None else os.getenv("GITLAB_TOKEN", "")

        if ssl_verify is not None:
            self.ssl_verify = ssl_verify
        else:
            self.ssl_verify = os.getenv("GITLAB_SSL_VERIFY", "false").lower() in ("true", "1", "yes")

    def _require_auth(self) -> None:
        if not self.api_url:
            raise ValueError("Не задан GITLAB_URL в .env (например: https://gitlab.t-bank.ru).")
        if not self.token:
            raise PermissionError("Для работы с GitLab укажите GITLAB_TOKEN (Personal Access Token) в .env.")

    def _headers(self) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.token,
            "User-Agent": "personal-assistant",
            "Content-Type": "application/json",
        }

    def _encode_id(self, project: str | int) -> str:
        # В GitLab API путь проекта вида 'group/repo' должен быть URL-encoded в 'group%2Frepo'
        return urllib.parse.quote(str(project).strip(), safe="")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._require_auth()
        with httpx.Client(base_url=self.api_url, headers=self._headers(), timeout=18.0,
                          verify=self.ssl_verify) as client:
            resp = client.request(method, path, **kwargs)
            if resp.status_code >= 400:
                raise RuntimeError(f"GitLab API {resp.status_code}: {resp.text[:400]}")
            return resp.json() if resp.text.strip() else {}

    def search_projects(self, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "membership": "true",
            "per_page": max(1, min(20, limit)),
            "order_by": "last_activity_at",
        }
        if query and query.strip():
            params["search"] = query.strip()

        data = self._request("GET", "/projects", params=params)
        return [
            {
                "id": p["id"],
                "name": p["name"],
                "path_with_namespace": p["path_with_namespace"],
                "description": p.get("description"),
                "default_branch": p.get("default_branch", "main"),
                "web_url": p["web_url"],
                "last_activity_at": p.get("last_activity_at"),
            }
            for p in data
        ]

    def get_project_info(self, project: str | int) -> dict[str, Any]:
        p = self._request("GET", f"/projects/{self._encode_id(project)}")
        return {
            "id": p["id"],
            "path_with_namespace": p["path_with_namespace"],
            "description": p.get("description"),
            "default_branch": p.get("default_branch", "main"),
            "web_url": p["web_url"],
            "star_count": p.get("star_count", 0),
            "open_issues_count": p.get("open_issues_count", 0),
        }

    def list_files(
            self,
            project: str | int,
            path: str = "",
            ref: str | None = None,
    ) -> list[dict[str, Any]]:
        pid = self._encode_id(project)
        params: dict[str, Any] = {"per_page": 50}
        if path.strip("/"):
            params["path"] = path.strip("/")
        if ref:
            params["ref"] = ref

        data = self._request("GET", f"/projects/{pid}/repository/tree", params=params)
        return [
            {
                "name": item["name"],
                "path": item["path"],
                "type": item["type"],  # 'blob' (файл) или 'tree' (папка)
            }
            for item in data
        ]

    def read_file(
            self,
            project: str | int,
            file_path: str,
            ref: str | None = None,
    ) -> dict[str, Any]:
        pid = self._encode_id(project)
        fpath = urllib.parse.quote(file_path.strip("/"), safe="")
        params = {"ref": ref} if ref else {}
        data = self._request("GET", f"/projects/{pid}/repository/files/{fpath}", params=params)

        raw = data.get("content", "")
        encoding = data.get("encoding", "base64")
        if encoding == "base64":
            content = base64.b64decode(raw).decode("utf-8", errors="replace")
        else:
            content = raw

        return {
            "file_name": data.get("file_name"),
            "file_path": data.get("file_path"),
            "size": data.get("size"),
            "ref": data.get("ref"),
            "content": content[:9000],
        }

    def list_commits(
            self,
            project: str | int,
            ref: str | None = None,
            limit: int = 8,
    ) -> list[dict[str, Any]]:
        pid = self._encode_id(project)
        params: dict[str, Any] = {"per_page": max(1, min(20, limit))}
        if ref:
            params["ref_name"] = ref
        items = self._request("GET", f"/projects/{pid}/repository/commits", params=params)
        return [
            {
                "short_id": c["short_id"],
                "title": c["title"],
                "author_name": c["author_name"],
                "created_at": c["created_at"],
            }
            for c in items
        ]

    def list_merge_requests(
            self,
            project: str | int,
            state: str = "opened",
            limit: int = 8,
    ) -> list[dict[str, Any]]:
        pid = self._encode_id(project)
        params = {"state": state, "per_page": max(1, min(15, limit))}
        items = self._request("GET", f"/projects/{pid}/merge_requests", params=params)
        return [
            {
                "iid": mr["iid"],
                "title": mr["title"],
                "state": mr["state"],
                "source_branch": mr["source_branch"],
                "target_branch": mr["target_branch"],
                "author": mr["author"]["username"],
                "web_url": mr["web_url"],
            }
            for mr in items
        ]

    def create_branch(
            self,
            project: str | int,
            new_branch: str,
            from_ref: str = "main",
    ) -> dict[str, Any]:
        pid = self._encode_id(project)
        res = self._request(
            "POST",
            f"/projects/{pid}/repository/branches",
            json={"branch": new_branch, "ref": from_ref},
        )
        return {"name": res["name"], "commit": res["commit"]["short_id"]}

    def upsert_file(
            self,
            project: str | int,
            file_path: str,
            content: str,
            commit_message: str,
            branch: str = "main",
    ) -> dict[str, Any]:
        pid = self._encode_id(project)
        clean_path = file_path.strip("/")
        encoded_fpath = urllib.parse.quote(clean_path, safe="")

        # Проверяем, существует ли файл в этой ветке
        file_exists = False
        try:
            self._request(
                "GET",
                f"/projects/{pid}/repository/files/{encoded_fpath}",
                params={"ref": branch},
            )
            file_exists = True
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise

        action = "update" if file_exists else "create"
        payload = {
            "branch": branch,
            "commit_message": commit_message,
            "actions": [
                {
                    "action": action,
                    "file_path": clean_path,
                    "content": content,
                }
            ],
        }

        res = self._request("POST", f"/projects/{pid}/repository/commits", json=payload)
        return {
            "status": "updated" if file_exists else "created",
            "commit_id": res.get("short_id"),
            "branch": branch,
            "file_path": clean_path,
            "web_url": res.get("web_url"),
        }

    def create_merge_request(
            self,
            project: str | int,
            source_branch: str,
            target_branch: str = "main",
            title: str = "Merge Request",
            description: str = "",
    ) -> dict[str, Any]:
        pid = self._encode_id(project)
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        mr = self._request("POST", f"/projects/{pid}/merge_requests", json=payload)
        return {
            "iid": mr["iid"],
            "title": mr["title"],
            "state": mr["state"],
            "web_url": mr["web_url"],
        }

    def get_latest_pipeline(
            self,
            project: str | int,
            ref: str | None = None,
    ) -> dict[str, Any]:
        """Проверяет статус сборки/тестов CI/CD пайплайна (супер-полезно для проверки дз в Т-Академии)."""
        pid = self._encode_id(project)
        params: dict[str, Any] = {"per_page": 1}
        if ref:
            params["ref"] = ref

        pipelines = self._request("GET", f"/projects/{pid}/pipelines", params=params)
        if not pipelines:
            return {"status": "no_pipelines", "message": "Пайплайны не найдены."}

        p_id = pipelines[0]["id"]
        detail = self._request("GET", f"/projects/{pid}/pipelines/{p_id}")
        jobs = self._request("GET", f"/projects/{pid}/pipelines/{p_id}/jobs")

        jobs_summary = [
            {"name": j["name"], "stage": j["stage"], "status": j["status"]}
            for j in jobs[:8]
        ]

        return {
            "pipeline_id": detail["id"],
            "status": detail["status"],  # success, failed, running, pending
            "ref": detail.get("ref"),
            "web_url": detail.get("web_url"),
            "created_at": detail.get("created_at"),
            "duration_seconds": detail.get("duration"),
            "jobs": jobs_summary,
        }


def register_gitlab_tools(
        registry: ToolRegistry, client: GitLabClient | None = None
) -> None:
    gl = client or GitLabClient()

    registry.register(
        name="gitlab_search_projects",
        description="Ищет проекты/репозитории в GitLab Т-Банка. Если query пустой — возвращает все ваши проекты.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос (оставьте пустым '', чтобы получить все доступные проекты).",
                },
                "limit": {"type": "integer", "description": "Количество результатов (по умолчанию 10)."},
            },
            "required": [],  # <--- теперь НЕ обязательно придумывать query
        },
        handler=gl.search_projects,
    )

    registry.register(
        name="gitlab_project_info",
        description="Возвращает информацию о проекте в GitLab (путь 'group/project_name' или ID).",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Путь к проекту (например 'python-course/lab1') или ID."}
            },
            "required": ["project"],
        },
        handler=gl.get_project_info,
    )

    registry.register(
        name="gitlab_list_files",
        description="Показывает файлы и папки в репозитории GitLab.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Путь к проекту или ID."},
                "path": {"type": "string", "description": "Относительный путь к подпапке (по умолчанию корень '')."},
                "ref": {"type": "string", "description": "Ветка (по умолчанию default branch)."},
            },
            "required": ["project"],
        },
        handler=gl.list_files,
    )

    registry.register(
        name="gitlab_read_file",
        description="Читает содержимое файла из репозитория GitLab.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Путь к проекту или ID."},
                "file_path": {"type": "string", "description": "Путь к файлу в репозитории."},
                "ref": {"type": "string", "description": "Ветка (по умолчанию default branch)."},
            },
            "required": ["project", "file_path"],
        },
        handler=gl.read_file,
    )

    registry.register(
        name="gitlab_list_commits",
        description="Показывает последние коммиты в репозитории GitLab.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Путь к проекту или ID."},
                "ref": {"type": "string", "description": "Имя ветки (опционально)."},
                "limit": {"type": "integer"},
            },
            "required": ["project"],
        },
        handler=gl.list_commits,
    )

    registry.register(
        name="gitlab_list_merge_requests",
        description="Показывает Merge Requests (MR) в проекте GitLab.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "state": {"type": "string", "enum": ["opened", "closed", "merged", "all"]},
                "limit": {"type": "integer"},
            },
            "required": ["project"],
        },
        handler=gl.list_merge_requests,
    )

    registry.register(
        name="gitlab_create_branch",
        description="Создаёт новую ветку в репозитории GitLab.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "new_branch": {"type": "string"},
                "from_ref": {"type": "string", "description": "Базовая ветка (по умолчанию main)."},
            },
            "required": ["project", "new_branch"],
        },
        handler=gl.create_branch,
    )

    registry.register(
        name="gitlab_upsert_file",
        description="Создаёт или обновляет файл в репозитории GitLab с коммитом.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "file_path": {"type": "string"},
                "content": {"type": "string"},
                "commit_message": {"type": "string"},
                "branch": {"type": "string", "description": "Ветка (по умолчанию main)."},
            },
            "required": ["project", "file_path", "content", "commit_message"],
        },
        handler=gl.upsert_file,
    )

    registry.register(
        name="gitlab_create_merge_request",
        description="Создаёт новый Merge Request в GitLab.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "source_branch": {"type": "string"},
                "target_branch": {"type": "string", "description": "Целевая ветка (по умолчанию main)."},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["project", "source_branch", "title"],
        },
        handler=gl.create_merge_request,
    )

    registry.register(
        name="gitlab_get_latest_pipeline",
        description="Проверяет статус последнего CI/CD пайплайна (сборка, тесты, линтеры) в проекте GitLab.",
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "ref": {"type": "string", "description": "Ветка (опционально)."},
            },
            "required": ["project"],
        },
        handler=gl.get_latest_pipeline,
    )
