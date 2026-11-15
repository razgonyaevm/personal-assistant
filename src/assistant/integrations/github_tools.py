from __future__ import annotations

import base64
import os
from typing import Any
import httpx

from assistant.tools import ToolRegistry

from dotenv import load_dotenv

load_dotenv()


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: str | None = None) -> None:
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "personal-assistant",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        with httpx.Client(
                base_url=self.BASE_URL, headers=self._headers(), timeout=15.0
        ) as client:
            resp = client.request(method, path, **kwargs)
            if resp.status_code >= 400:
                raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text[:400]}")
            return resp.json()

    def _require_token(self) -> None:
        if not self.token:
            raise PermissionError(
                "Для этой операции требуется GITHUB_TOKEN в файле .env."
            )

    # --- Поиск и чтение ---

    def search_repos(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/search/repositories",
            params={"q": query, "sort": "stars", "per_page": max(1, min(10, limit))},
        )
        return [
            {
                "full_name": r["full_name"],
                "stars": r["stargazers_count"],
                "language": r.get("language"),
                "description": r.get("description"),
                "url": r["html_url"],
            }
            for r in data.get("items", [])
        ]

    def get_repo_info(self, owner: str, repo: str) -> dict[str, Any]:
        r = self._request("GET", f"/repos/{owner}/{repo}")
        return {
            "full_name": r["full_name"],
            "private": r["private"],
            "default_branch": r["default_branch"],
            "stars": r["stargazers_count"],
            "open_issues_count": r["open_issues_count"],
            "language": r.get("language"),
            "description": r.get("description"),
            "updated_at": r["updated_at"],
            "url": r["html_url"],
        }

    def list_files(
            self, owner: str, repo: str, path: str = "", ref: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"ref": ref} if ref else {}
        clean_path = path.strip("/")
        data = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{clean_path}", params=params
        )
        if not isinstance(data, list):
            return [{"name": data["name"], "path": data["path"], "type": data["type"]}]
        return [
            {
                "name": item["name"],
                "path": item["path"],
                "type": item["type"],
                "size": item.get("size", 0),
            }
            for item in data
        ]

    def read_file(
            self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> dict[str, Any]:
        params = {"ref": ref} if ref else {}
        data = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path.strip('/')}", params=params
        )
        if isinstance(data, list):
            raise ValueError(f"'{path}' — это директория. Используйте github_list_files.")
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return {
            "path": data["path"],
            "sha": data["sha"],
            "content": content[:8000],
        }

    def list_commits(
            self, owner: str, repo: str, branch: str | None = None, limit: int = 8
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": max(1, min(20, limit))}
        if branch:
            params["sha"] = branch
        items = self._request("GET", f"/repos/{owner}/{repo}/commits", params=params)
        return [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].splitlines()[0],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in items
        ]

    def list_issues(
            self,
            owner: str,
            repo: str,
            state: str = "open",
            limit: int = 10,
    ) -> list[dict[str, Any]]:
        items = self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": max(1, min(20, limit))},
        )
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "is_pr": "pull_request" in i,
                "author": i["user"]["login"],
                "url": i["html_url"],
            }
            for i in items
        ]

    # --- Запись: ветки, файлы с коммитом, PR, issues ---

    def create_branch(
            self,
            owner: str,
            repo: str,
            new_branch: str,
            from_branch: str = "main",
    ) -> dict[str, Any]:
        self._require_token()
        ref_data = self._request(
            "GET", f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}"
        )
        sha = ref_data["object"]["sha"]
        created = self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{new_branch}", "sha": sha},
        )
        return {"ref": created["ref"], "sha": created["object"]["sha"][:7]}

    def upsert_file(
            self,
            owner: str,
            repo: str,
            path: str,
            content: str,
            message: str,
            branch: str = "main",
    ) -> dict[str, Any]:
        self._require_token()
        clean_path = path.strip("/")

        existing_sha: str | None = None
        try:
            existing = self._request(
                "GET",
                f"/repos/{owner}/{repo}/contents/{clean_path}",
                params={"ref": branch},
            )
            if isinstance(existing, dict):
                existing_sha = existing.get("sha")
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise

        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        res = self._request(
            "PUT", f"/repos/{owner}/{repo}/contents/{clean_path}", json=payload
        )
        return {
            "status": "updated" if existing_sha else "created",
            "path": res["content"]["path"],
            "branch": branch,
            "commit_sha": res["commit"]["sha"][:7],
            "url": res["content"]["html_url"],
        }

    def list_my_repos(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        self._require_token()
        repos = self._request(
            "GET",
            "/user/repos",
            params={
                "sort": "updated",
                "per_page": 50,
                "affiliation": "owner,collaborator,organization_member",
            },
        )
        q = query.strip().lower()
        if q:
            repos = [
                r
                for r in repos
                if q in r["full_name"].lower()
                   or q in (r.get("description") or "").lower()
            ]
        return [
            {
                "full_name": r["full_name"],
                "private": r["private"],
                "default_branch": r["default_branch"],
                "updated_at": r["updated_at"],
                "url": r["html_url"],
            }
            for r in repos[: max(1, min(30, limit))]
        ]

    def list_pull_requests(
            self,
            owner: str,
            repo: str,
            state: str = "open",
            limit: int = 10,
    ) -> list[dict[str, Any]]:
        prs = self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": max(1, min(20, limit))},
        )
        return [
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "draft": pr.get("draft", False),
                "head": pr["head"]["ref"],
                "base": pr["base"]["ref"],
                "merged_at": pr.get("merged_at"),
                "author": pr["user"]["login"],
                "url": pr["html_url"],
            }
            for pr in prs
        ]

    def create_pull_request(
            self,
            owner: str,
            repo: str,
            title: str,
            head: str,
            base: str = "main",
            body: str = "",
    ) -> dict[str, Any]:
        self._require_token()
        pr = self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        return {
            "number": pr["number"],
            "title": pr["title"],
            "state": pr["state"],
            "url": pr["html_url"],
        }

    def create_issue(
            self,
            owner: str,
            repo: str,
            title: str,
            body: str = "",
    ) -> dict[str, Any]:
        self._require_token()
        issue = self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues",
            json={"title": title, "body": body},
        )
        return {
            "number": issue["number"],
            "title": issue["title"],
            "url": issue["html_url"],
        }


def register_github_tools(
        registry: ToolRegistry, gh: GitHubClient | None = None
) -> None:
    client = gh or GitHubClient()

    registry.register(
        name="github_search_repos",
        description="Ищет репозитории на GitHub по запросу.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос."},
                "limit": {"type": "integer", "description": "Количество (по умолчанию 5)."},
            },
            "required": ["query"],
        },
        handler=client.search_repos,
    )

    registry.register(
        name="github_repo_info",
        description="Возвращает информацию о GitHub-репозитории (owner/repo).",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
        handler=client.get_repo_info,
    )

    registry.register(
        name="github_list_files",
        description="Показывает список файлов и папок в директории GitHub-репозитория.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string", "description": "Путь к папке ('' для корня)."},
                "ref": {"type": "string", "description": "Ветка или SHA (опционально)."},
            },
            "required": ["owner", "repo"],
        },
        handler=client.list_files,
    )

    registry.register(
        name="github_read_file",
        description="Читает содержимое файла из GitHub-репозитория.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "ref": {"type": "string", "description": "Ветка или SHA (опционально)."},
            },
            "required": ["owner", "repo", "path"],
        },
        handler=client.read_file,
    )

    registry.register(
        name="github_list_commits",
        description="Показывает последние коммиты в GitHub-репозитории.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "branch": {"type": "string", "description": "Имя ветки (опционально)."},
                "limit": {"type": "integer"},
            },
            "required": ["owner", "repo"],
        },
        handler=client.list_commits,
    )

    registry.register(
        name="github_list_issues",
        description="Показывает issues и PR в GitHub-репозитории.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer"},
            },
            "required": ["owner", "repo"],
        },
        handler=client.list_issues,
    )

    registry.register(
        name="github_create_branch",
        description="Создаёт новую ветку в GitHub-репозитории.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "new_branch": {"type": "string"},
                "from_branch": {"type": "string", "description": "От какой ветки (по умолчанию main)."},
            },
            "required": ["owner", "repo", "new_branch"],
        },
        handler=client.create_branch,
    )

    registry.register(
        name="github_upsert_file",
        description=(
            "Создаёт или обновляет файл в GitHub-репозитории и делает коммит. "
            "Для безопасности лучше коммитить в отдельную ветку, а не сразу в main."
        ),
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "message": {"type": "string", "description": "Commit message."},
                "branch": {"type": "string", "description": "Ветка (по умолчанию main)."},
            },
            "required": ["owner", "repo", "path", "content", "message"],
        },
        handler=client.upsert_file,
    )

    registry.register(
        name="github_create_pull_request",
        description="Создаёт Pull Request из ветки head в ветку base.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "head": {"type": "string", "description": "Ветка с изменениями."},
                "base": {"type": "string", "description": "Целевая ветка (по умолчанию main)."},
                "body": {"type": "string"},
            },
            "required": ["owner", "repo", "title", "head"],
        },
        handler=client.create_pull_request,
    )

    registry.register(
        name="github_create_issue",
        description="Создаёт новое issue в GitHub-репозитории.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["owner", "repo", "title"],
        },
        handler=client.create_issue,
    )

    registry.register(
        name="github_list_my_repos",
        description=(
            "Показывает личные, закрытые и доступные пользовательские репозитории на GitHub "
            "(по текущему GITHUB_TOKEN). Всегда используй этот инструмент, когда пользователь спрашивает про СВОЙ репозиторий."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Фильтр по названию (опционально, например 'xv6')."},
                "limit": {"type": "integer", "description": "Сколько репозиториев вернуть (по умолчанию 20)."},
            },
            "required": [],
        },
        handler=client.list_my_repos,
    )

    registry.register(
        name="github_list_pull_requests",
        description="Показывает Pull Requests (ветки head/base, статус, merged) в GitHub-репозитории.",
        parameters={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer"},
            },
            "required": ["owner", "repo"],
        },
        handler=client.list_pull_requests,
    )
