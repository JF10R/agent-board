"""Execute published examples against isolated stores."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from agent_board import cli, web

ROOT = Path(__file__).resolve().parents[1]


def block(path, language):
    return re.search(r"```" + language + r"\n(.*?)\n```", (ROOT / path).read_text(encoding="utf-8"), re.S).group(1)


def test_documented_push_listener(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    command = [sys.executable, "-B", str(ROOT / "agent_board.py"), "--repo", str(repo)]
    subprocess.run(command + ["init"], check=True, capture_output=True, env=env)
    body = tmp_path / "body.md"
    body.write_text("Documented watcher test.", encoding="utf-8")
    subprocess.run(command + ["post", "--from", "master", "--to", "lead", "--kind", "STATUS", "--workstream", "demo", "--summary", "Watcher example", "--body-file", str(body)], check=True, capture_output=True, env=env)
    source = block("docs/push-listener.md", "python")
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    exec(compile(source, "docs/push-listener.md", "exec"), {"REPO": str(repo), "ACTOR": "lead"})
    assert "Watcher example" in capsys.readouterr().out


def test_documented_http_request(tmp_path, capsys):
    root = cli.initialize(tmp_path / "board")
    cli.seed_project_config(root)
    server = web.create_server(root, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = block("docs/http-api.md", "python").replace("http://127.0.0.1:8765", f"http://127.0.0.1:{server.server_port}")
        exec(compile(source, "docs/http-api.md", "exec"), {})
        assert "Example ticket" in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_reference_matches_parser():
    spec = importlib.util.spec_from_file_location("generate_cli_reference", ROOT / "docs/generate_cli_reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (ROOT / "docs/cli-reference.md").read_text(encoding="utf-8") == module.render()



def test_documented_ticket_workflow(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    exec(compile(block("docs/ticket-workflow.md", "python"), "docs/ticket-workflow.md", "exec"), {})
    assert "Workflow completed" in capsys.readouterr().out
