"""Tiny stdlib HTTP server: POST /v1/decide (alias /v1/systemone), GET /v1/stats, GET /healthz."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .schema import SchemaError

MAX_BODY = 4 * 1024 * 1024


def make_handler(gateway, quiet=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "decide-gateway"

        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True, "backends": list(gateway.backends)})
            if self.path == "/v1/stats":
                return self._send(200, gateway.stats_snapshot())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/v1/decide", "/v1/systemone"):
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(400, {"error": "missing or oversized body"})
            try:
                req = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"error": "body is not valid JSON"})
            try:
                return self._send(200, gateway.decide(req))
            except SchemaError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # keep serving; report the failure to the caller
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt, *args):
            if not quiet:
                super().log_message(fmt, *args)

    return Handler


def serve(gateway, host="127.0.0.1", port=8787, quiet=False):
    httpd = ThreadingHTTPServer((host, port), make_handler(gateway, quiet))
    return httpd
