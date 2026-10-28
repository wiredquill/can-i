"""End-to-end sandbox tests: run can-i inside a locked-down SUSE BCI container.

Opt-in: requires RUN_CONTAINER_TESTS=1 and podman or docker.
    RUN_CONTAINER_TESTS=1 pytest tests/ -m container
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "can-i-e2e:test"

RUNTIME = next((r for r in ("podman", "docker") if shutil.which(r)), None)

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(
        os.environ.get("RUN_CONTAINER_TESTS") != "1",
        reason="set RUN_CONTAINER_TESTS=1 to run containerized e2e tests",
    ),
    pytest.mark.skipif(RUNTIME is None, reason="needs podman or docker"),
]


@pytest.fixture(scope="module")
def image():
    proc = subprocess.run(
        [RUNTIME, "build", "-t", IMAGE, "-f", "Dockerfile", "."],
        cwd=ROOT, capture_output=True, text=True, timeout=1200,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    yield IMAGE
    subprocess.run([RUNTIME, "image", "rm", "-f", IMAGE], capture_output=True)


def run_in_container(image: str, extra_args: list[str]) -> dict:
    proc = subprocess.run(
        [RUNTIME, "run", "--rm", "--cap-drop", "ALL", image, "run", "--json", *extra_args],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def test_privileged_actions_denied_in_locked_down_container(image):
    body = run_in_container(image, [])
    by_id = {r["id"]: r for r in body["results"]}

    # running as uid 10001 with all capabilities dropped:
    assert by_id["fs-read-shadow"]["status"] == "denied"
    # nmap ships in the image
    assert "not found" not in by_id["net-nmap-suse"]["stderr"]

    # benign baseline actions still work:
    assert by_id["fs-write-tmp"]["status"] == "allowed"
    assert by_id["proc-spawn-sh"]["status"] == "allowed"

    # sandbox introspection notices the container + dropped caps:
    sysinfo = body["system"]
    assert sysinfo["uid"] == 10001
    assert sysinfo["container_runtime"] in ("docker", "podman", "oci")
    assert "SYS_ADMIN" not in sysinfo["capabilities"]
    assert sysinfo["sandboxed"] is True
