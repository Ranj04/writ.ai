from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ack_uses_only_the_decision_id_column(tmp_path: Path) -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            payload: object
            if self.path == "/health":
                payload = {"status": "ok"}
            else:
                payload = {
                    "sessions": [
                        {
                            "session_id": "session-1",
                            "task_id": "TASK-1",
                            "assignment_id": "assignment-1",
                            "source": "registered",
                            "cwd": str(tmp_path),
                            "decision_id": "DEC-018",
                            "bound": True,
                            "snapshot_current": False,
                            "deny_spent": False,
                            "state": "interrupted",
                            "decision_snapshot": "graph-v17",
                            "current_decision_snapshot": "graph-v18",
                        }
                    ]
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            requests.append(self.path)
            body = json.dumps({"acknowledged": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        completed = subprocess.run(
            ["bash", "scripts/demo/ack.sh", "--yes"],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "WRITAI_DEMO_AGENT_PORT": str(server.server_port),
                "WRITAI_DEMO_ROOT": str(tmp_path / "demo-root"),
            },
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "TASK-1  blocked by DEC-018  (session session-1)" in completed.stdout
    assert "yesno" not in completed.stdout
    assert requests == ["/supervisor/sessions/session-1/acknowledge"]
