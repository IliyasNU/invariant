"""A stand-in for the GitHub API, for the Compose smoke test.

    python3 github_stub.py [port]

Pull request 7 of every repository has one changed file,
``app/domain/orders.py``, which imports ``app.infrastructure.db``. Comments
written on it are kept, and ``GET /_comments`` lists them. Only the standard
library is used.
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HEAD = "1" * 40
BASE = "2" * 40
FILES = {"app/domain/orders.py": b"import app.infrastructure.db\n"}
REPOSITORY = r"/repos/[^/]+/[^/]+"
comments: dict[int, str] = {}


class GitHub(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode())

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/_comments":
            return self._json(200, [comments[key] for key in sorted(comments)])
        if match := re.fullmatch(REPOSITORY + r"/pulls/(\d+)", url.path):
            return self._json(
                200,
                {
                    "number": int(match[1]),
                    "head": {"sha": HEAD},
                    "base": {"sha": BASE},
                    "changed_files": len(FILES),
                },
            )
        if re.fullmatch(REPOSITORY + r"/pulls/\d+/files", url.path):
            page = int(parse_qs(url.query).get("page", ["1"])[0])
            files = [{"filename": name, "status": "added"} for name in FILES]
            return self._json(200, files if page == 1 else [])
        if match := re.fullmatch(REPOSITORY + r"/contents/(.+)", url.path):
            content = FILES.get(unquote(match[1]))
            if content is not None and parse_qs(url.query).get("ref") == [HEAD]:
                return self._send(200, content, "application/vnd.github.raw")
        return self._json(404, {"message": "Not Found"})

    def do_POST(self) -> None:
        if re.fullmatch(REPOSITORY + r"/issues/\d+/comments", self.path):
            comment_id = len(comments) + 1
            comments[comment_id] = self._body()["body"]
            return self._json(201, {"id": comment_id})
        return self._json(404, {"message": "Not Found"})

    def do_PATCH(self) -> None:
        match = re.fullmatch(REPOSITORY + r"/issues/comments/(\d+)", self.path)
        if match and int(match[1]) in comments:
            comments[int(match[1])] = self._body()["body"]
            return self._json(200, {"id": int(match[1])})
        return self._json(404, {"message": "Not Found"})

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, file=sys.stderr, flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    ThreadingHTTPServer(("0.0.0.0", port), GitHub).serve_forever()
