from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "external_plugin"


class InstalledPluginTests(unittest.TestCase):
    def test_external_entrypoint_persists_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, cwd=ROOT)
            python = venv / "bin" / "python"
            subprocess.run([str(python), "-m", "pip", "install", "--no-deps", ".", str(FIXTURE)], check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
            program = """
import http.client
import pathlib
import tempfile
import threading
import sys
import agent_telemetry
from agent_telemetry.adapters import load_adapter
from agent_telemetry.server import TelemetryHTTPServer
from agent_telemetry.storage import Storage

assert pathlib.Path(agent_telemetry.__file__).is_relative_to(sys.prefix)
path = pathlib.Path.cwd() / 'telemetry.sqlite'
storage = Storage(path)
storage.initialize()
server = TelemetryHTTPServer(('127.0.0.1', 0), load_adapter('fixture'), storage, 1024, 1.0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
connection.request('POST', '/v1/logs', b'{\"fixture\":true}', {'Content-Type': 'application/json'})
response = connection.getresponse()
assert response.status == 200 and response.read() == b'{}'
connection.close()
server.shutdown()
server.server_close()
thread.join()
assert storage.query()[0]['platform'] == 'fixture'

claude_path = pathlib.Path.cwd() / 'claude.sqlite'
claude_storage = Storage(claude_path)
claude_storage.initialize()
claude_server = TelemetryHTTPServer(('127.0.0.1', 0), load_adapter('claude'), claude_storage, 1024, 1.0)
claude_thread = threading.Thread(target=claude_server.serve_forever, daemon=True)
claude_thread.start()
claude_payload = b'{"resourceLogs":[{"resource":{"attributes":[{"key":"app.version","value":{"stringValue":"1.2.3"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"7","attributes":[{"key":"event.name","value":{"stringValue":"tool_result"}},{"key":"session.id","value":{"stringValue":"installed-session"}}]}]}]}]}'
connection = http.client.HTTPConnection('127.0.0.1', claude_server.server_port, timeout=2)
connection.request('POST', '/v1/logs', claude_payload, {'Content-Type': 'application/json'})
response = connection.getresponse()
assert response.status == 200 and response.read() == b'{}'
connection.close()
claude_server.shutdown()
claude_server.server_close()
claude_thread.join()
rows = Storage(claude_path, readonly=True).query(session='installed-session')
assert len(rows) == 1
assert rows[0]['platform'] == 'claude' and rows[0]['event_name'] == 'claude_code.tool_result'
"""
            subprocess.run([str(python), "-I", "-c", program], check=True, cwd=tmp)
