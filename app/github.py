import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas import SourceFile
from invariant import __version__
from invariant.files import MAX_FILE_BYTES, decode_source

MAX_FILE_PAGES = 30
# Bodies are read up to these limits and no further.
MAX_RESPONSE_BYTES = 10_000_000  # one page of metadata or of the file list
MAX_TOTAL_BYTES = 50_000_000  # every file of one pull request
# A Git commit ID: SHA-1, or SHA-256 for repositories that use it.
COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
FILES_PER_PAGE = 100


@dataclass(frozen=True)
class PullRequestMetadata:
    number: int
    head_sha: str
    # GitHub's own count of changed files, used to detect a truncated file list.
    changed_files: int | None = None
    base_sha: str | None = None


@dataclass(frozen=True)
class SkippedFile:
    path: str
    reason: Literal["too_large", "undecodable", "invalid_path", "limit_exceeded"]


@dataclass(frozen=True)
class ChangedPythonFiles:
    files: list[SourceFile]
    # Changed Python files that could not be analyzed.
    skipped: list[SkippedFile]
    # GitHub listed fewer changed files than the pull request has.
    truncated: bool
    # Removed and non-Python files, which are outside the analysis.
    ignored: int

    @property
    def complete(self) -> bool:
        return not self.skipped and not self.truncated


@dataclass
class GitHubAPIError(Exception):
    status_code: int
    code: str
    message: str
    # Worth retrying later: the network, a rate limit, a GitHub server error.
    transient: bool = False
    # Seconds GitHub asked to wait before the next request, if it said.
    retry_after: float | None = None


def _retry_after(response: httpx.Response) -> float | None:
    """The wait GitHub asks for: Retry-After, or until X-RateLimit-Reset."""
    retry_after = response.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return float(retry_after)
    reset = response.headers.get("X-RateLimit-Reset", "")
    if reset.isdigit():
        return max(0.0, int(reset) - time.time())
    return None


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
            "User-Agent": f"invariant/{__version__}",
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
            # A renamed or transferred repository answers with a redirect.
            # httpx drops the token when a redirect leaves the API's origin.
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _send(
        self, method: str, path: str, *, limit: int, **kwargs: Any
    ) -> bytes | None:
        """A successful response's body, or None if it is longer than ``limit``.

        The body is streamed and abandoned at the limit, so an oversized response
        never reaches memory.
        """
        try:
            with self._client.stream(method, path, **kwargs) as response:
                _check_status(response)
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > limit:
                    return None
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > limit:
                        return None
                return bytes(body)
        except httpx.RequestError as exc:
            raise GitHubAPIError(
                status_code=503,
                code="github_unavailable",
                message="GitHub could not be reached",
                transient=True,
            ) from exc

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        body = self._send(method, path, limit=MAX_RESPONSE_BYTES, **kwargs)
        if body is None:
            raise GitHubAPIError(
                status_code=502,
                code="github_invalid_response",
                message="GitHub returned a response over the size limit",
            )
        try:
            return json.loads(body)
        except ValueError as exc:
            raise GitHubAPIError(
                status_code=502,
                code="github_invalid_response",
                message="GitHub returned an invalid response",
            ) from exc

    def get_pull_request(
        self, owner: str, repo: str, pull_number: int
    ) -> PullRequestMetadata:
        payload = self._json("GET", f"/repos/{owner}/{repo}/pulls/{pull_number}")
        try:
            number = payload["number"]
            head_sha = payload["head"]["sha"]
            changed_files = payload.get("changed_files")
            base = payload.get("base")
            base_sha = base.get("sha") if isinstance(base, dict) else None
        except (AttributeError, KeyError, TypeError) as exc:
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
            or not COMMIT_ID.fullmatch(head_sha)
        ):
            raise GitHubAPIError(
                status_code=502,
                code="github_invalid_response",
                message="GitHub pull request metadata was invalid",
            )
        if (
            not isinstance(changed_files, int)
            or isinstance(changed_files, bool)
            or changed_files < 0
        ):
            changed_files = None
        if not isinstance(base_sha, str) or not COMMIT_ID.fullmatch(base_sha):
            base_sha = None  # only recorded, so a missing one is not fatal
        return PullRequestMetadata(
            number=number,
            head_sha=head_sha,
            changed_files=changed_files,
            base_sha=base_sha,
        )

    def get_changed_files(
        self, owner: str, repo: str, pull_number: int
    ) -> list[dict[str, Any]]:
        changed_files: list[dict[str, Any]] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            payload = self._json(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
                params={"per_page": FILES_PER_PAGE, "page": page},
            )
            # A malformed entry could hide a Python file, so it fails the request.
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) and isinstance(item.get("filename"), str)
                for item in payload
            ):
                raise GitHubAPIError(
                    status_code=502,
                    code="github_invalid_response",
                    message="GitHub changed-files response was invalid",
                )
            changed_files.extend(payload)
            if len(payload) < FILES_PER_PAGE:
                break
        return changed_files

    def get_file_bytes(
        self, owner: str, repo: str, path: str, ref: str
    ) -> bytes | None:
        """The file at ``ref``, or None if it is larger than ``MAX_FILE_BYTES``."""
        encoded_path = quote(path, safe="/")
        return self._send(
            "GET",
            f"/repos/{owner}/{repo}/contents/{encoded_path}",
            limit=MAX_FILE_BYTES,
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )

    def get_changed_python_files(
        self,
        owner: str,
        repo: str,
        pull_request: PullRequestMetadata,
        *,
        deadline: float | None = None,
    ) -> ChangedPythonFiles:
        """The changed Python files at the pull request's head commit.

        Files are read only at ``pull_request.head_sha``. If the pull request
        moved to another commit while its files were listed, the list may
        belong to that commit, so the analysis is retried rather than mixed.
        Past ``deadline`` (a ``time.monotonic()`` value) or ``MAX_TOTAL_BYTES``,
        the remaining files are reported instead of downloaded.
        """
        entries = self.get_changed_files(owner, repo, pull_request.number)
        latest = self.get_pull_request(owner, repo, pull_request.number)
        if latest.head_sha != pull_request.head_sha:
            raise GitHubAPIError(
                status_code=409,
                code="github_pull_request_changed",
                message="The pull request changed while its files were listed",
                transient=True,
            )
        if pull_request.changed_files is not None:
            truncated = len(entries) < pull_request.changed_files
        else:
            truncated = len(entries) >= MAX_FILE_PAGES * FILES_PER_PAGE

        files: list[SourceFile] = []
        skipped: list[SkippedFile] = []
        ignored = 0
        budget = MAX_TOTAL_BYTES
        for entry in entries:
            path = entry["filename"]
            if entry.get("status") in {"removed", "deleted"} or not path.endswith(
                ".py"
            ):
                ignored += 1
                continue
            try:
                # Validate the path before downloading anything for it.
                SourceFile(path=path, content="")
            except ValidationError:
                skipped.append(SkippedFile(path, "invalid_path"))
                continue
            if truncated:
                # The result is incomplete anyway; spare the downloads.
                continue
            if budget <= 0 or (deadline is not None and time.monotonic() > deadline):
                skipped.append(SkippedFile(path, "limit_exceeded"))
                continue
            content = self.get_file_bytes(owner, repo, path, pull_request.head_sha)
            if content is None:
                skipped.append(SkippedFile(path, "too_large"))
                continue
            budget -= len(content)
            try:
                text = decode_source(content)
            except UnicodeDecodeError:
                skipped.append(SkippedFile(path, "undecodable"))
                continue
            files.append(SourceFile(path=path, content=text))
        return ChangedPythonFiles(
            files=files, skipped=skipped, truncated=truncated, ignored=ignored
        )

    def create_comment(self, owner: str, repo: str, pull_number: int, body: str) -> int:
        """Comment on the pull request; returns the new comment's ID."""
        payload = self._json(
            "POST",
            f"/repos/{owner}/{repo}/issues/{pull_number}/comments",
            json={"body": body},
        )
        comment_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(comment_id, int) or isinstance(comment_id, bool):
            raise GitHubAPIError(
                status_code=502,
                code="github_invalid_response",
                message="GitHub did not return the new comment's ID",
            )
        return comment_id

    def update_comment(self, owner: str, repo: str, comment_id: int, body: str) -> bool:
        """Replace a comment's text; False if the comment no longer exists."""
        try:
            self._json(
                "PATCH",
                f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
                json={"body": body},
            )
        except GitHubAPIError as exc:
            if exc.code == "github_not_found":
                return False
            raise
        return True


def _check_status(response: httpx.Response) -> None:
    """Raise the error a GitHub response stands for, judged by status and headers."""
    if response.status_code < 300:
        return
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
            transient=True,
            retry_after=_retry_after(response),
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
        transient=response.status_code >= 500,
    )


def verify_webhook_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    # As bytes: a header may hold any character, and compare_digest refuses a
    # str that is not ASCII.
    return hmac.compare_digest(expected.encode(), signature.encode())
