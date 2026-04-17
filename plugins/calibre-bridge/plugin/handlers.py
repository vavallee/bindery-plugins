import json
from http.server import BaseHTTPRequestHandler

PLUGIN_VERSION = '0.2.0'


def _calibre_version() -> str:
    try:
        from calibre.constants import numeric_version
        return '.'.join(str(p) for p in numeric_version[:3])
    except Exception:
        return 'unknown'


def make_handler(api_key: str, get_db):
    from calibre_plugins.bindery_bridge.plugin.adder import add_book

    class Handler(BaseHTTPRequestHandler):
        server_version = 'BinderyBridge/' + PLUGIN_VERSION

        def log_message(self, format, *args):  # noqa: A002
            return

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            if not api_key:
                return True
            header = self.headers.get('Authorization', '')
            if not header.startswith('Bearer '):
                return False
            return header[len('Bearer '):].strip() == api_key

        def do_GET(self):  # noqa: N802
            if self.path == '/v1/health':
                db = get_db()
                library = ''
                if db is not None:
                    try:
                        library = db.library_path
                    except Exception:
                        library = ''
                self._send_json(200, {
                    'plugin_version': PLUGIN_VERSION,
                    'calibre_version': _calibre_version(),
                    'library': library,
                })
                return
            self._send_json(404, {'error': 'not found'})

        def do_POST(self):  # noqa: N802
            if self.path != '/v1/books':
                self._send_json(404, {'error': 'not found'})
                return
            if not self._check_auth():
                self._send_json(401, {'error': 'unauthorized'})
                return
            db = get_db()
            if db is None:
                self._send_json(503, {'error': 'library not ready'})
                return
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length > 0 else b''
            try:
                payload = json.loads(raw.decode('utf-8')) if raw else {}
            except ValueError:
                self._send_json(400, {'error': 'invalid json'})
                return
            path = payload.get('path')
            if not path or not isinstance(path, str):
                self._send_json(400, {'error': 'path required'})
                return
            try:
                book_id, duplicate = add_book(db, path)
            except FileNotFoundError as exc:
                self._send_json(400, {'error': str(exc)})
                return
            except Exception as exc:  # pragma: no cover - defensive
                self._send_json(500, {'error': str(exc)})
                return
            status = 409 if duplicate else 201
            self._send_json(status, {'id': int(book_id), 'duplicate': bool(duplicate)})

    return Handler
