"""HTTP front for ui/api.py. Binds to this computer only: python -m ui.server"""
import argparse
import json
import os
import re
import shutil
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from novel_project import NOVELS_ROOT_ENV  # noqa: E402
from ui.api import App, Request  # noqa: E402
from ui.worker import run_scheduler  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
_STATIC_NAME = re.compile(r"^[a-z0-9_-]+\.(html|css|js|svg)$")
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}


class Handler(BaseHTTPRequestHandler):
    server_version = "NovelTranslator"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # Nothing about requests is logged, so a key or a novel's text can't end up in a log.

    @property
    def app(self):
        return self.server.app

    def _host_ok(self):
        """Refuse any other Host, so a website that rebinds its name to 127.0.0.1 can't reach this."""
        port = self.server.server_address[1]
        return self.headers.get("Host", "").lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _same_origin(self):
        return self.headers.get("Origin", "").lower() == f"http://{self.headers.get('Host', '').lower()}"

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _send_file(self, path, name):
        self.send_response(200)
        self.send_header("Content-Type", "application/epub+zip")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

    def _static(self):
        path = self.path.split("?", 1)[0]
        name = "index.html" if path == "/" else path[len("/static/"):] if path.startswith("/static/") else ""
        target = STATIC_DIR / name
        if not _STATIC_NAME.match(name) or target.resolve().parent != STATIC_DIR.resolve() or not target.is_file():
            return self._send_json(404, {"error": "Not found."})
        self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix])

    def _api(self, method):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return self._send_json(400, {"error": "Bad request."})
        if length < 0 or self.headers.get("Transfer-Encoding"):
            return self._send_json(400, {"error": "Bad request."})
        try:
            response = self.app.handle(Request(method, self.path, self.rfile, length))
        except Exception:
            # The traceback goes to this app's window only. The browser gets no detail.
            traceback.print_exc()
            return self._send_json(500, {"error": "Something went wrong. Details are in the app's window."})
        if response.file_path is not None:
            return self._send_file(response.file_path, response.file_name)
        self._send_json(response.status, response.value)

    def do_GET(self):
        if not self._host_ok():
            return self._send_json(403, {"error": "Forbidden."})
        if self.path.startswith("/api/"):
            return self._api("GET")
        self._static()

    def do_POST(self):
        if not self._host_ok() or not self._same_origin():
            return self._send_json(403, {"error": "Forbidden."})
        if not self.path.startswith("/api/"):
            return self._send_json(404, {"error": "Not found."})
        self._api("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows the default would let a second copy share the port without an error.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port, app):
        super().__init__((HOST, port), Handler)
        self.app = app


def main(argv=None):
    parser = argparse.ArgumentParser(description="NovelTranslator's local web interface")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="Open the browser once the server is ready")
    parser.add_argument("--novels-root", type=Path, help="Use this novels folder instead of the configured one")
    parser.add_argument("--demo", action="store_true",
                        help="Synthetic folder only: a fake worker, no paid requests, a separate key file")
    args = parser.parse_args(argv)
    if args.novels_root:
        os.environ[NOVELS_ROOT_ENV] = str(args.novels_root.resolve())
    if args.demo:
        from ui.demo import demo_app
        try:
            app = demo_app(REPO_ROOT)
        except ValueError as exc:
            print(exc)
            return 2
    else:
        app = App(REPO_ROOT)
    url = f"http://{HOST}:{args.port}/"
    try:
        server = Server(args.port, app)
    except OSError:
        print(f"Port {args.port} is already in use, so the app is probably already open: {url}")
        if args.open:
            webbrowser.open(url)
        return 0
    stop = threading.Event()
    threading.Thread(target=run_scheduler, args=(app.tick, stop), daemon=True).start()
    print(f"NovelTranslator is open at {url}", flush=True)
    print("Closing this window closes the app. A translation already running keeps going.")
    print("The queue only moves on while this app is open.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
