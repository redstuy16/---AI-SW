import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from probe.real_tools import ToolRegistry
from probe.sandbox import PythonSandboxTool, docker_available
from probe.schemas import ContextRef, RefType

from test_real_tools import real_context, imported, request


def docker_image_ready():
    if not docker_available():
        return False
    return subprocess.run(["docker", "image", "inspect", PythonSandboxTool.image],
                          capture_output=True, check=False).returncode == 0


requires_docker = pytest.mark.skipif(not docker_image_ready(),
                                     reason="Docker daemon or built sandbox image unavailable")


def sandbox_setup(real_context):
    _, state, _, _, research_id, contract, task_id = real_context
    registry = ToolRegistry(state)
    tool = PythonSandboxTool(state, contract.contract_id)
    registry.register(tool)
    return registry, tool, research_id, contract, task_id


def test_docker_unavailable_fails_closed(real_context, monkeypatch):
    registry, _, research_id, contract, task_id = sandbox_setup(real_context)
    monkeypatch.setattr("probe.sandbox.docker_available", lambda: False)
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "python.execute", {"code": "print(1+1)"}))
    assert not result.ok
    assert result.error.startswith("DOCKER_UNAVAILABLE")
    assert not list((real_context[2].research_root(research_id) / "scripts").glob("*.py"))


def test_docker_command_is_restricted(real_context):
    _, tool, research_id, _, _ = sandbox_setup(real_context)
    base = real_context[2].research_root(research_id)
    command = tool.build_command(base / "scripts" / "ART-123-analysis.py", base / "results", [])
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--user=65534:65534" in command
    assert "--cap-drop=ALL" in command
    assert "--memory=512m" in command
    assert "--pids-limit=64" in command
    assert "--pull=never" in command
    assert all(".env" not in item for item in command)


def test_sandbox_rejects_untrusted_artifact_id(real_context):
    registry, _, research_id, contract, task_id = sandbox_setup(real_context)
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "python.execute",
        {"code": "print(1)", "input_artifact_refs": [{"type": "artifact", "id": "../../escape"}]}))
    assert not result.ok
    assert result.error.startswith("INVALID_INPUT_ARTIFACT")


def test_timeout_error_mapping_and_container_cleanup(real_context, monkeypatch):
    registry, _, research_id, contract, task_id = sandbox_setup(real_context)
    monkeypatch.setattr("probe.sandbox.docker_available", lambda: True)
    calls = []

    original = subprocess.Popen
    children = []
    def calculation(command, **kwargs):
        child = original([sys.executable, '-B', '-X', 'utf8', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("probe.sandbox_io.subprocess.Popen", calculation)
    monkeypatch.setattr("probe.sandbox.subprocess.run", fake_run)
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "python.execute",
        {"code": "while True: pass", "timeout_sec": 1}))
    assert not result.ok and result.error.startswith("TIMEOUT")
    assert any(command[1:3] == ["rm", "-f"] for command in calls)
    assert children and all(child.poll() is not None for child in children)


@pytest.mark.docker_integration
@requires_docker
def test_docker_normal_execution_and_structured_output(real_context):
    dataset_id = imported(real_context)
    registry, _, research_id, contract, task_id = sandbox_setup(real_context)
    code = "import csv,json\nprint(1+1)\nwith open('/work/input/" + dataset_id + ".csv') as f:\n rows=list(csv.DictReader(f))\nwith open('/work/output/result.json','w') as f:\n json.dump({'n':len(rows),'temperature_mean':sum(float(r['temperature']) for r in rows)/len(rows)},f)\n"
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "python.execute",
        {"code": code, "input_artifact_refs": [{"type": "dataset", "id": dataset_id}]}))
    assert result.ok, result.error
    assert result.result["structured_result"] == {"n": 8, "temperature_mean": 17.0}
    assert result.result["stdout"].strip() == "2"
    assert len(result.result["generated_artifacts"]) == 1
    assert len(result.artifacts) == 2


@pytest.mark.docker_integration
@requires_docker
def test_docker_timeout(real_context):
    registry, _, research_id, contract, task_id = sandbox_setup(real_context)
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "python.execute",
        {"code": "while True: pass", "timeout_sec": 1}))
    assert not result.ok and result.error.startswith("TIMEOUT")


@pytest.mark.docker_integration
@requires_docker
def test_docker_network_is_blocked(real_context):
    registry, _, research_id, contract, task_id = sandbox_setup(real_context)
    code = "import socket,json\ns=socket.socket()\ntry:\n s.connect(('1.1.1.1',80))\n blocked=False\nexcept OSError:\n blocked=True\nwith open('/work/output/result.json','w') as f: json.dump({'network_blocked':blocked},f)\n"
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "python.execute", {"code": code}))
    assert result.ok, result.error
    assert result.result["structured_result"]["network_blocked"] is True


@pytest.mark.docker_integration
@requires_docker
def test_docker_host_path_is_hidden(real_context, tmp_path):
    registry, _, research_id, contract, task_id = sandbox_setup(real_context)
    secret = tmp_path / "host-secret.txt"
    content = "unmounted secret"
    content.encode("utf-8", errors="strict")
    secret.write_text(content, encoding="utf-8")
    code = "import os,json\nwith open('/work/output/result.json','w') as f: json.dump({'host_visible':os.path.exists(" + repr(str(secret)) + ")},f)\n"
    result = registry.dispatch(contract.contract_id, request(research_id, task_id, "python.execute", {"code": code}))
    assert result.ok, result.error
    assert result.result["structured_result"]["host_visible"] is False
