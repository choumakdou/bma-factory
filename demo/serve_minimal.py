"""Minimal container: a stdlib HTTP server on $PORT, nothing else.

Used to isolate "can this image run at all on Cloud Run" from "does the
bma-factory app work". If this comes up and the app does not, the problem
is in the app. If this also fails, the problem is the image or the
runtime.
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"bma-factory minimal container OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    print(f"[minimal] python {sys.version.split()[0]} listening on 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
