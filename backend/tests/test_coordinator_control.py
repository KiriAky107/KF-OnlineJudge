import asyncio
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from uuid import uuid4

from kf_oj.infra.coordinator import CoordinatorControl


def test_real_loopback_cancellation_notification_and_failure():
    token, evaluation = secrets.token_hex(32), uuid4()

    class Handler(BaseHTTPRequestHandler):
        code = 204
        seen = []

        def do_POST(self):
            assert self.path == "/internal/cancel"
            assert self.headers["Authorization"] == "Bearer " + token
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body == {
                "schema_version": 1,
                "kind": "judge.evaluation",
                "evaluation_id": str(evaluation),
            }
            self.seen.append(body)
            self.send_response(self.code)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    control = CoordinatorControl(f"http://127.0.0.1:{server.server_port}/", token)
    try:
        assert asyncio.run(control.cancel(evaluation)) is True
        Handler.code = 503
        assert asyncio.run(control.cancel(evaluation)) is False
        assert len(Handler.seen) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert asyncio.run(control.cancel(evaluation)) is False
