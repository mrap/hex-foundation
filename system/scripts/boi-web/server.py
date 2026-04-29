"""
BOI live status web view.

Serves a single-page HTML with a Server-Sent Events stream that pushes
`boi status --json` output every 2 seconds. Runs under serve.sh over
HTTPS on the Tailscale hostname.

No external dependencies beyond the standard library.
"""

from __future__ import annotations

import http.server
import json
import os
import ssl
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "index.html"


def fetch_status() -> dict:
    """Run `boi status --json` and return parsed dict. Never raises."""
    try:
        result = subprocess.run(
            ["bash", os.path.expanduser("~/.boi/boi"), "status", "--json", "--all"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode != 0:
            return {"error": f"boi exited {result.returncode}", "stderr": result.stderr[:500]}
        return json.loads(result.stdout)
    except Exception as exc:
        return {"error": repr(exc)}


class BoiHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet logs
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = INDEX_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    payload = fetch_status()
                    payload["_timestamp"] = time.time()
                    data = json.dumps(payload).encode("utf-8")
                    self.wfile.write(b"data: " + data + b"\n\n")
                    self.wfile.flush()
                    time.sleep(2.0)
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        if self.path == "/api/status.json":
            payload = fetch_status()
            payload["_timestamp"] = time.time()
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()


def main():
    port = int(os.environ.get("PORT", "8891"))
    cert = os.environ.get(
        "CERT", ""
    )
    key = os.environ.get(
        "KEY", ""
    )

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), BoiHandler)
    if cert and key and Path(cert).exists() and Path(key).exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print(f"BOI live status (TLS) → https://localhost:{port}", flush=True)
    else:
        print(f"BOI live status (no TLS) → http://localhost:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
