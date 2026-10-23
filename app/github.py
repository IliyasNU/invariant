import hashlib
import hmac
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas import SourceFile

MAX_GITHUB_FILE_BYTES = 1_000_000
MAX_FILE_PAGES = 30


@dataclass(frozen=True)
class PullRequestMetadata:
    number: int
    head_sha: str


@dataclass
class GitHubAPIError(Exception):
    status_code: int
    code: str
    message: str


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "invariant/0.2",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"

        self.comment_enabled = bool(
            settings.github_post_pr_comment and settings.github_token
        )
        self._client = httpx.Client(
            base_url=settings.github_api_url.rstrip("/"),
            headers=headers,
            timeout=15,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise GitHubAPIError(
                status_code=503,
                code="github_unavailable",
                message="GitHub could not be reached",
            ) from exc

        if response.status_code < 400:
            return response
        if response.status_code == 404:
            raise GitHubAPIError(
                status_code=404,
                code="github_not_found",
                message="GitHub repository, pull request, or file was not found",
            )
        if response.status_code == 429 or (
            response.status_code == 403
            and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "Retry-After" in response.headers
            )
        ):
            raise GitHubAPIError(
                status_code=429,
                code="github_rate_limited",
                message="GitHub API rate limit exceeded",
            )
        if response.status_code in {401, 403}:
            raise GitHubAPIError(
                status_code=502,
                code="github_authentication_failed",
                message="GitHub rejected the configured credentials or permissions",
            )
        raise GitHubAPIError(
            status_code=502,
            code="github_api_error",
            message=f"GitHub API returned status {response.status_code}",
        )

    def _json(self, path: str, **kwargs: Any) -> Any:
        response = self._request("GET", path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubAPIError(
                status_code=502,
                code="github_invalid_response",
                message="GitHub returned an invalid response",
            ) from exc

    def get_pull_request(
        self, owner: str, repo: str, pull_number: int
    ) -> PullRequestMetadata:
        payload = self._json(f"/repos/{owner}/{repo}/pulls/{pull_number}")
        try:
            number = payload["number"]
            head_sha = payload["head"]["sha"]
        except (KeyError, TypeError) as exc:
            raise GitHubAPIError(
                status_code=502,
                code="github_invalid_response",
                message="GitHub pull request metadata was incomplete",
            ) from exc
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
            or not isinstance(head_sha, str)
            or not head_sha
        ):
            raise GitHubAPIError(
                status_code=502,
                code="github_invalid_response",
                message="GitHub pull request metadata was invalid",
            )
        return PullRequestMetadata(number=number, head_sha=head_sha)

    def get_changed_files(
        self, owner: str, repo: str, pull_number: int
    ) -> list[dict[str, Any]]:
        changed_files: list[dict[str, Any]] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            payload = self._json(
                f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
                params={"per_page": 100, "page": page},
            )
            if not isinstance(payload, list):
                raise GitHubAPIError(
                    status_code=502,
                    code="github_invalid_response",
                    message="GitHub changed-files response was invalid",
                )
            changed_files.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                break
        return changed_files

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str
    ) -> str | None:
        encoded_path = quote(path, safe="/")
        response = self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{encoded_path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if len(response.content) > MAX_GITHUB_FILE_BYTES:
            return None
        try:
            return response.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None

    def get_changed_python_files(
        self, owner: str, repo: str, pull_number: int, head_sha: str
    ) -> list[SourceFile]:
        sources: list[SourceFile] = []
        for changed_file in self.get_changed_files(owner, repo, pull_number):
            path = changed_file.get("filename")
            file_status = changed_file.get("status")
            if (
                not isinstance(path, str)
                or not path.endswith(".py")
                or len(path) > 500
                or path.startswith("/")
                or ".." in path.split("/")
                or file_status in {"removed", "deleted"}
            ):
                continue
            content = self.get_file_content(owner, repo, path, head_sha)
            if content is None:
                continue
            try:
                sources.append(SourceFile(path=path, content=content))
            except ValidationError:
                # Git allows paths the API rejects, such as pkg\..\mod.py.
                continue
        return sources

    def post_pull_request_comment(
        self, owner: str, repo: str, pull_number: int, body: str
    ) -> None:
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{pull_number}/comments",
            json={"body": body},
        )


def get_github_client() -> Generator[GitHubClient]:
    with GitHubClient(Settings()) as client:
        yield client


def verify_webhook_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)
