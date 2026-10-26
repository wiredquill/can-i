"""Render charts/can-i with helm: image dropdown, generated OpenShell policy."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from cani.policy import policy_to_checks

CHART = Path(__file__).parent.parent / "charts" / "can-i"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render(tmp_path, **values) -> list[dict]:
    vals = tmp_path / "values.yaml"
    vals.write_text(json.dumps(values))
    out = subprocess.run(
        ["helm", "template", "demo", str(CHART), "-f", str(vals)],
        check=True, capture_output=True, text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def kind(docs, k):
    return next(d for d in docs if d["kind"] == k)


def env(docs) -> dict[str, str]:
    container = kind(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"]}


@pytest.mark.parametrize("choice,image", [
    ("suse", "ghcr.io/wiredquill/can-i:latest"),
    ("opensuse", "ghcr.io/wiredquill/can-i-opensuse:latest"),
    ("nvidia", "ghcr.io/wiredquill/can-i-nvidia:latest"),
])
def test_image_dropdown_picks_sandbox_image(tmp_path, choice, image):
    assert env(render(tmp_path, sandbox={"image": choice}))["SANDBOX_IMAGE"] == image


def test_custom_image_requires_a_reference(tmp_path):
    docs = render(tmp_path, sandbox={"image": "custom", "customImage": "reg.example/can-i:dev"})
    assert env(docs)["SANDBOX_IMAGE"] == "reg.example/can-i:dev"
    with pytest.raises(subprocess.CalledProcessError) as exc:
        render(tmp_path, sandbox={"image": "custom"})
    assert "customImage is required" in exc.value.stderr


def test_policy_is_a_valid_openshell_policy(tmp_path):
    docs = render(tmp_path, policy={"egress": "suse.com:443, opensuse.org", "extraReadWrite": "/data"})
    text = kind(docs, "ConfigMap")["data"]["policy.yaml"]
    policy = yaml.safe_load(text)
    assert "/data" in policy["filesystem_policy"]["read_write"]
    assert "/opt" in policy["filesystem_policy"]["read_only"]  # venv in the opensuse/nvidia images
    assert policy["process"]["run_as_user"] == "10001"
    endpoints = policy["network_policies"]["can-i-egress"]["endpoints"]
    assert endpoints == [{"host": "suse.com", "port": 443}, {"host": "opensuse.org", "port": 443}]
    policy_to_checks(text)  # can-i's own policy reader accepts it


def test_blank_egress_denies_all_network(tmp_path):
    policy = yaml.safe_load(kind(render(tmp_path, policy={"egress": ""}), "ConfigMap")["data"]["policy.yaml"])
    assert "network_policies" not in policy


def test_policy_or_image_change_restarts_the_client(tmp_path):
    def annotations(**values):
        return kind(render(tmp_path, **values), "Deployment")["spec"]["template"]["metadata"]["annotations"]

    base = annotations()
    assert annotations(sandbox={"image": "nvidia"})["checksum/image"] != base["checksum/image"]
    assert annotations(policy={"egress": "opensuse.org:443"})["checksum/policy"] != base["checksum/policy"]


def test_gateway_installs_from_git_without_dependency_build(tmp_path):
    """Rancher's git ClusterRepo packages the chart directory as committed:
    the vendored NVIDIA chart must be there, with the form-friendly SAN patch."""
    gateway = CHART.parent / "openshell-gateway"
    subprocess.run(["helm", "package", str(gateway), "-d", str(tmp_path)],
                   check=True, capture_output=True)
    (tgz,) = tmp_path.glob("openshell-gateway-*.tgz")
    vals = tmp_path / "values.yaml"
    vals.write_text(json.dumps({"openshell": {
        "agentSandbox": {"preflight": {"enabled": False}},
        "service": {"type": "NodePort"},
        "pkiInitJob": {"extraServerSans": "10.0.9.50, openshell.lab.example.com"},
    }}))
    out = subprocess.run(["helm", "template", "openshell", str(tgz), "-f", str(vals)],
                         check=True, capture_output=True, text=True).stdout
    assert "--server-san=10.0.9.50" in out
    assert "--server-san=openshell.lab.example.com" in out
    services = [d for d in yaml.safe_load_all(out) if d and d["kind"] == "Service"]
    assert any(s["metadata"]["name"] == "openshell" and s["spec"]["type"] == "NodePort" for s in services)
