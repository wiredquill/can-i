"""Bidirectional interop between OpenShell policy YAML and can-i checks.yaml.

policy -> checks: the policy IS the test plan. Grants become probes with
`expect: allowed`; implied default-deny behavior becomes control probes with
`expect: denied`.

checks -> policy: checks expecting `allowed` (or carrying no expectation, which
means "I want this to work") become grants. Checks expecting `denied` are
omitted — in OpenShell, absence from the allowlists IS the denial.
"""

from __future__ import annotations

import ipaddress
import os
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
        raw_binaries = policy.get("binaries") or ["/usr/bin/curl"]
        # schema form is {"path": ...}; tolerate plain strings on import
        binaries = [
            b.get("path", "") if isinstance(b, dict) else str(b) for b in raw_binaries
        ]
        for endpoint in policy.get("endpoints") or []:
            if isinstance(endpoint, dict):
                check = _endpoint_check(endpoint, binaries[0] if binaries else "curl")
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
    """The executable's real path: OpenShell matches binaries by the path the
    kernel reports, after symlinks (python3 -> python3.13)."""
    path = token if "/" in token else shutil.which(token) or f"/usr/bin/{token}"
    return os.path.realpath(path)


def _tokens(command: str) -> list[str]:
    """The command's argv, looking through a `sh -c '...'` wrapper."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    if len(tokens) >= 3 and tokens[0] in ("sh", "bash", "/bin/sh", "/bin/bash") and tokens[1] == "-c":
        return _tokens(tokens[2].split("|")[0].split("&&")[0])
    return tokens


def _first_token(command: str) -> str:
    tokens = _tokens(command)
    return tokens[0] if tokens else ""


def _nmap_endpoints(command: str) -> list[dict]:
    """host:port pairs an `nmap -p<ports> <host>` probe connects to."""
    tokens = _tokens(command)
    if not tokens or tokens[0].rsplit("/", 1)[-1] != "nmap":
        return []
    ports: list[int] = []
    hosts: list[str] = []
    args = iter(tokens[1:])
    for t in args:
        if t == "-p":
            t = "-p" + next(args, "")
        if t.startswith("-p"):
            ports += [int(p) for p in t[2:].split(",") if p.isdigit()]
        elif not t.startswith("-"):
            hosts.append(t)
    return [{"host": h, "port": p} for h in hosts for p in ports]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


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
            for endpoint in _nmap_endpoints(check.command):
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
        # OpenShell schema: binaries are objects, {"path": "/usr/bin/curl"}
        binary_objs = [{"path": b} for b in sorted(binaries)] or [{"path": "/usr/bin/curl"}]
        policy["network_policies"] = {
            "can-i-egress": {
                "name": "can-i-egress",
                "endpoints": endpoints,
                "binaries": binary_objs,
            }
        }

    out = yaml.safe_dump(policy, sort_keys=False, default_flow_style=False)
    if omitted:
        out += "\n# Denied-by-omission checks (no grant emitted — absence denies):\n"
        for oid in omitted:
            out += f"#   - {oid}\n"
    return out


# ---------------------------------------------------------------------------
# single denied check -> how to allow it
# ---------------------------------------------------------------------------

FIX_RULE_NAME = "can-i-egress"  # the rule checks_to_policy emits


def fix_for_check(check: Check, sandbox: str, blocker_kind: str | None = None) -> dict:
    """Explain how to flip one check to ALLOWED under OpenShell.

    Returns {"command", "yaml", "note", "live"}: `command` is the openshell CLI
    invocation (None when no policy change can grant it), `yaml` the policy
    fragment that grants it, `live` whether the change hot-reloads.
    """
    granted = check.model_copy(update={"expect": None})
    fragment = checks_to_policy(Config(checks=[granted]))
    policy = yaml.safe_load(fragment) or {}
    has_grant = "filesystem_policy" in policy or "network_policies" in policy
    sb = shlex.quote(sandbox)

    if blocker_kind == "baseline":
        note = ("This denial is not the sandbox's doing — it is denied without a sandbox too "
                "(missing binary, unix permissions, no route). A policy change alone won't flip it.")
    else:
        note = ""

    limit = _hard_limit(check, policy)
    if limit:
        return {"command": None, "yaml": None, "live": False, "limit": True, "note": note or limit}

    if not has_grant:
        return {
            "command": None,
            "yaml": None,
            "live": False,
            "note": note or ("OpenShell policy can't express a grant for this probe — only filesystem "
                             "paths and HTTP(S) egress endpoints are policy-controlled. Raw sockets, "
                             "capabilities and syscalls come from the sandbox runtime itself."),
        }

    if "network_policies" in policy:
        rule = policy["network_policies"][FIX_RULE_NAME]
        binary = rule["binaries"][0]["path"]
        # the rule is named after the command (python3), not the versioned
        # file it resolves to (python3.13)
        tool = _first_token(check.command).rsplit("/", 1)[-1] or binary.rsplit("/", 1)[-1]
        # Each binary gets its own rule, never the chart's can-i-egress: adding
        # an endpoint to a rule changes the access of every binary already in
        # it, which OpenShell refuses unless they are all declared.
        rule_name = f"can-i-{_slug(tool)}"
        # curl's probes are GETs, so its endpoints get read-only REST. Other
        # binaries get no HTTP inspection: nmap sends no HTTP, and package
        # tools may need more than read-only methods.
        rest = ":read-only:rest:enforce" if tool == "curl" else ""
        lines = [
            f"openshell policy update {sb} \\\n"
            f"  --rule-name {rule_name} --binary {shlex.quote(binary)} \\\n"
            f"  --add-endpoint {ep['host']}:{ep['port']}"
f"{rest} --wait"
            for ep in rule["endpoints"]
        ]
        return {
            "command": "\n".join(lines),
            "yaml": fragment,
            "live": True,
            "note": note or "Network rules hot-reload — the card flips on the next run.",
        }

    # filesystem grants are fixed at sandbox creation
    return {
        "command": (
            "# merge the YAML fragment into your policy file (policy.yaml), then recreate:\n"
            f"openshell sandbox delete {sb}\n"
            f"openshell sandbox create --name {sb} \\\n"
            "  --from ghcr.io/wiredquill/can-i:latest \\\n"
            "  --policy policy.yaml \\\n"
            "  --detach -- can-i web --host 0.0.0.0 --port 8000"
        ),
        "yaml": fragment,
        "live": False,
        "note": note or ("Filesystem policy is fixed when the sandbox is created. With the can-i app, "
                         "add the paths to Extra read-write paths (or Extra read-only paths) in its form "
                         "and upgrade, which recreates the sandbox. Otherwise merge the fragment into your "
                         "policy file and recreate the sandbox as shown."),
    }


def _hard_limit(check: Check, policy: dict) -> str | None:
    """Why no OpenShell policy can grant this check, or None if one can."""
    token = _first_token(check.command).rsplit("/", 1)[-1]
    if token in ("ping", "ping6"):
        return ("No OpenShell policy can grant ping. The sandbox has no CAP_NET_RAW and its supervisor "
                "only proxies TCP, so ICMP never leaves it. To show a grant, use curl to the same host: "
                "one policy update flips that check.")
    if token == "unshare":
        return ("No OpenShell policy can grant this. Every OpenShell sandbox runs under a seccomp filter "
                "that blocks namespace creation, so an agent can't build its own container to escape.")
    rule = (policy.get("network_policies") or {}).get(FIX_RULE_NAME) or {}
    ips = [ep["host"] for ep in rule.get("endpoints", []) if _is_ip(ep["host"])]
    if ips:
        meta = any(ipaddress.ip_address(h).is_link_local for h in ips)
        return ("No OpenShell policy can grant this. Egress rules name hosts, not raw IP addresses"
                + (", and the link-local cloud metadata service stays blocked in every sandbox" if meta else "")
                + ". Use a hostname to show a grant.")
    return None
