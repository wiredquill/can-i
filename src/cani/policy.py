"""Bidirectional interop between OpenShell policy YAML and can-i checks.yaml.

policy -> checks: the policy IS the test plan. Grants become probes with
`expect: allowed`; implied default-deny behavior becomes control probes with
`expect: denied`.

checks -> policy: checks expecting `allowed` (or carrying no expectation, which
means "I want this to work") become grants. Checks expecting `denied` are
omitted — in OpenShell, absence from the allowlists IS the denial.
"""

from __future__ import annotations

import re
import shlex
import shutil
from urllib.parse import urlparse

import yaml

from .config import Check, Config

CONTROL_HOST = "example.org"  # unlisted-endpoint control; edit after generating


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]+", "-", text)).strip("-")


# ---------------------------------------------------------------------------
# policy -> checks
# ---------------------------------------------------------------------------


def _endpoint_check(endpoint: dict, binary: str) -> Check | None:
    host = endpoint.get("host")
    if not host:
        return None
    port = endpoint.get("port")
    scheme = "http" if port == 80 else "https"
    url = f"{scheme}://{host}"
    if port and port not in (80, 443):
        url += f":{port}"
    tool = binary.rsplit("/", 1)[-1] if "/" in binary else binary
    if tool != "curl":
        command = f"{tool} {url}"  # best effort for non-curl binaries
    else:
        command = f"curl -fsS --max-time 3 -o /dev/null {url}"
    return Check(
        id=f"net-allow-{_slug(host)}",
        name=f"Egress allowed: {host}",
        category="network",
        command=command,
        expect="allowed",
        description=f"Granted by network policy endpoint {host}",
    )


def policy_to_checks(policy_text: str) -> Config:
    """Derive a checks.yaml Config from an OpenShell policy document."""
    data = yaml.safe_load(policy_text)
    if not isinstance(data, dict):
        raise ValueError("policy must be a YAML mapping")

    checks: list[Check] = []

    fs = data.get("filesystem_policy") or {}
    for path in fs.get("read_only") or []:
        checks.append(
            Check(
                id=f"fs-read-{_slug(path)}",
                name=f"Read allowed: {path}",
                category="filesystem",
                command=f"test -r {path}",
                expect="allowed",
                description="Granted by filesystem_policy.read_only",
            )
        )
    for path in fs.get("read_write") or []:
        checks.append(
            Check(
                id=f"fs-write-{_slug(path)}",
                name=f"Write allowed: {path}",
                category="filesystem",
                command=f"test -w {path}",
                expect="allowed",
                description="Granted by filesystem_policy.read_write",
            )
        )
    if fs.get("include_workdir"):
        checks.append(
            Check(
                id="fs-read-workdir",
                name="Workdir is readable",
                category="filesystem",
                command="test -r .",
                expect="allowed",
                description="Granted by filesystem_policy.include_workdir",
            )
        )
    if fs:
        rw = set(fs.get("read_write") or [])
        if "/etc" not in rw:
            checks.append(
                Check(
                    id="fs-write-etc-control",
                    name="Write to /etc (control: not granted)",
                    category="filesystem",
                    command="touch /etc/.can-i-probe",
                    expect="denied",
                    description="/etc is not in filesystem_policy.read_write",
                )
            )

    proc = data.get("process") or {}
    if proc.get("run_as_user"):
        checks.append(
            Check(
                id="fs-read-shadow",
                name="Read /etc/shadow (control: non-root user)",
                category="filesystem",
                command="head -c1 /etc/shadow",
                expect="denied",
                description=f"process.run_as_user={proc['run_as_user']} cannot read shadow",
            )
        )

    net = data.get("network_policies") or {}
    for _policy_name, policy in net.items():
        if not isinstance(policy, dict):
            continue
        binaries = policy.get("binaries") or ["/usr/bin/curl"]
        for endpoint in policy.get("endpoints") or []:
            if isinstance(endpoint, dict):
                check = _endpoint_check(endpoint, binaries[0])
                if check:
                    checks.append(check)
    if net:
        checks.append(
            Check(
                id="net-deny-unlisted-control",
                name=f"Egress to unlisted host {CONTROL_HOST} (control)",
                category="network",
                command=f"curl -fsS --max-time 3 -o /dev/null https://{CONTROL_HOST}",
                expect="denied",
                description="Not granted by any network_policies endpoint",
            )
        )

    if not checks:
        raise ValueError("policy contains nothing can-i can derive probes from")
    return Config(checks=checks)


# ---------------------------------------------------------------------------
# checks -> policy
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s'\"]+")
_READ_RE = re.compile(r"(?:test\s+-r|cat|head\s+-c\d+)\s+(?P<path>/\S+)")
_WRITE_RE = re.compile(r"(?:test\s+-w\s+(?P<w1>/\S+))|(?:touch\s+(?P<w2>/\S+))")


def _binary_path(token: str) -> str:
    if "/" in token:
        return token
    return shutil.which(token) or f"/usr/bin/{token}"


def _first_token(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    return tokens[0] if tokens else ""


def checks_to_policy(config: Config) -> str:
    """Generate an OpenShell policy YAML document from a checks Config."""
    read_only: list[str] = []
    read_write: list[str] = []
    endpoints: list[dict] = []
    binaries: set[str] = set()
    omitted: list[str] = []

    def want_grant(check: Check) -> bool:
        # expect=allowed -> grant; no expectation -> the check exists because
        # the user wants the capability, so grant; expect=denied -> omit.
        if check.expect == "denied":
            omitted.append(check.id)
            return False
        return True

    for check in config.checks:
        if not want_grant(check):
            continue
        if check.category == "network":
            for url in _URL_RE.findall(check.command):
                parsed = urlparse(url)
                if not parsed.hostname:
                    continue
                endpoint: dict = {"host": parsed.hostname}
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                endpoint["port"] = port
                if endpoint not in endpoints:
                    endpoints.append(endpoint)
            token = _first_token(check.command)
            if token:
                binaries.add(_binary_path(token))
        elif check.category == "filesystem":
            for m in _WRITE_RE.finditer(check.command):
                path = m.group("w1") or m.group("w2")
                if not path:
                    continue
                # a write to a file grants its parent directory
                target = path if m.group("w1") else path.rsplit("/", 1)[0] or "/"
                if target not in read_write:
                    read_write.append(target)
            for m in _READ_RE.finditer(check.command):
                path = m.group("path")
                if path not in read_only and path not in read_write:
                    read_only.append(path)

    policy: dict = {"version": 1}
    if read_only or read_write:
        policy["filesystem_policy"] = {}
        if read_only:
            policy["filesystem_policy"]["read_only"] = read_only
        if read_write:
            policy["filesystem_policy"]["read_write"] = read_write
    if endpoints:
        policy["network_policies"] = {
            "can-i-egress": {
                "name": "can-i-egress",
                "endpoints": endpoints,
                "binaries": sorted(binaries) or ["/usr/bin/curl"],
            }
        }

    out = yaml.safe_dump(policy, sort_keys=False, default_flow_style=False)
    if omitted:
        out += "\n# Denied-by-omission checks (no grant emitted — absence denies):\n"
        for oid in omitted:
            out += f"#   - {oid}\n"
    return out
