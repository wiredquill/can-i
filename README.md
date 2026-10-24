# can-i?

**Security capability inspector and live sandbox demonstration dashboard** — dual-interface (TUI + Web).

`can-i` executes a battery of benign probe actions (network egress, sensitive file access, subprocess spawning, privilege-escalation attempts) and shows, in real time, which ones the current environment **allows** and which it **denies**. Apply an OpenShell sandbox profile, an AppArmor policy, or a seccomp filter and watch indicators flip from green to red — live, on stage.

- **TUI** — fast terminal dashboard (Textual), built for SSH sessions and `openshell sandbox connect`.
- **Web UI** — dark-mode dashboard (FastAPI + SSE) with a live `checks.yaml` editor; no restarts needed to change probes.
- **Everywhere** — runs from a SUSE BCI container image, or installs directly on a host/sandbox via [`mise`](https://mise.jdx.dev) like an agent payload.

```
 Host hermes   Kernel 6.12.0-160000.39-default   User erin (uid 1000)
 Container podman   AppArmor unconfined   seccomp filter   Sandboxed YES
┌─────────┬────────────────────────────┬────────────┬──────┬─────────┐
│ Status  │ Check                      │ Category   │ Exit │ Latency │
├─────────┼────────────────────────────┼────────────┼──────┼─────────┤
│ ALLOWED │ Outbound HTTPS (suse.com)  │ network    │ 0    │ 312 ms  │
│ DENIED  │ Read /etc/shadow           │ filesystem │ 1    │ 4 ms    │
│ DENIED  │ Raw ICMP socket (ping)     │ network    │ 2    │ 6 ms    │
└─────────┴────────────────────────────┴────────────┴──────┴─────────┘
```

## Probe categories

Probes are defined in [`checks.yaml`](checks.yaml) — one benign shell command per check. **Exit code 0 = ALLOWED, anything else = DENIED.**

| Category | Examples |
|---|---|
| `network` | outbound HTTPS, DNS lookups, raw-socket ping, cloud metadata endpoint |
| `filesystem` | read `/etc/shadow`, write `/etc` vs `/tmp`, exec from `noexec` mounts, `/proc/1/environ` |
| `process` | spawn `sh`/`curl`/`nmap`, `unshare` user namespaces, `sudo -n` |

## Quick start

### 1. Native / agent-local via mise (no container)

```bash
# one-line bootstrap: installs mise if needed, deps, and a can-i shim in ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/wiredquill/can-i/main/install.sh | bash

# or from a checkout:
mise install            # python 3.13 + uv from .mise.toml
mise run install        # create .venv, install can-i
mise run test           # full test suite
mise run start:tui      # terminal dashboard
mise run start:web      # web dashboard on :8000
```

### 2. Container (SUSE BCI)

The [Dockerfile](Dockerfile) is multi-stage and built **strictly on SUSE Base Container Images** (`registry.suse.com/bci/python:3.13`).

**Official images** live on GHCR, built by [`.github/workflows/container.yml`](.github/workflows/container.yml)
(the test suite gates the image build; multi-arch `linux/amd64` + `linux/arm64`):

```bash
podman pull ghcr.io/wiredquill/can-i:latest
podman run --rm -p 8000:8000 ghcr.io/wiredquill/can-i:latest
```

**Local dev loop** — build on your box (fastest) and share via the LAN registry:

```bash
mise run build:local        # podman build -t reg.wiredquill.com/repo/can-i:dev -t can-i:dev .
mise run run:local          # web dashboard on :8000 from the local image
mise run push:local         # push to reg.wiredquill.com (podman login first)
```

Direct podman, no mise:

```bash
podman build -t can-i .
podman run --rm -p 8000:8000 can-i                      # web dashboard
podman run --rm -it can-i tui                           # terminal dashboard
podman run --rm can-i run                               # one-shot probe report

# the demo moment — lock it down and watch probes flip to DENIED:
podman run --rm --cap-drop ALL --read-only can-i run
```

### 3. Running under OpenShell (the clean demo)

The most effective way to show OpenShell enforcement: **launch the can-i container image as
the workload of a child sandbox** with a policy attached. The dashboard then shows, live,
exactly what that policy allows and denies — no host changes, no container rebuilds.

```bash
# one-time: point the CLI at a gateway
openshell gateway add http://127.0.0.1:18080 --local --name local
openshell gateway select local

# launch the can-i image as the sandbox workload, policy attached at creation
openshell sandbox create --name can-i \
  --from ghcr.io/wiredquill/can-i:latest \
  --policy examples/policy-to-run-can-i.yaml \
  --detach -- can-i web --host 0.0.0.0 --port 8000

# or attach a terminal and drive the TUI yourself
openshell sandbox create --name can-i-tui \
  --from ghcr.io/wiredquill/can-i:latest \
  --policy examples/policy-to-run-can-i.yaml
```

[`examples/policy-to-run-can-i.yaml`](examples/policy-to-run-can-i.yaml) is the minimal grant
set: `/tmp` writable, `run_as_user: sandbox`, and egress only for `curl → suse.com:443`.
Everything the dashboard shows as DENIED (metadata endpoint, `/etc/shadow`, `/etc` writes,
nmap, sudo, raw ICMP) is denied *by omission* — OpenShell never authorizes
`169.254.169.254`, and raw ICMP isn't expressible in endpoint rules at all, so those badges
are red by construction.

The demo moment — change the network policy live while the dashboard runs (network rules
hot-reload; filesystem/process changes need a sandbox recreate):

```bash
# grant another destination: the matching badge flips DENIED -> ALLOWED
# (endpoint value = host:port:access:protocol:enforcement)
openshell policy update can-i \
  --rule-name can-i-egress --binary /usr/bin/curl \
  --add-endpoint opensuse.org:443:read-only:rest:enforce --wait

# pull the plug: every network badge goes red
openshell policy update can-i --remove-rule can-i-egress --wait

# verify what the sandbox actually enforces
openshell policy get can-i --full
```

Generate the equivalent policy from any can-i config with
`can-i to-policy --config checks.yaml`, or derive a test plan from an existing policy with
`can-i from-policy policy.yaml` — see [OpenShell policy interop](#openshell-policy-interop-both-directions).
You can also bake a policy into an image at `/etc/openshell/policy.yaml`; a `--policy` file
or `OPENSHELL_SANDBOX_POLICY` takes precedence.

Note: the web UI on :8000 is *inbound* traffic — sandbox policies govern egress and
filesystem, not port publishing. Reach the UI via the sandbox's connectivity options
(`openshell sandbox connect`, port forwarding), or just use the TUI over the attach session.

## Usage

```
can-i run  [--config checks.yaml] [--json]   # one-shot probe report (exit 0)
can-i tui  [--config checks.yaml]            # interactive terminal UI (r/space = run, q = quit)
can-i web  [--config checks.yaml] [--host 0.0.0.0] [--port 8000]
can-i from-policy POLICY.yaml [-o checks.yaml]  # OpenShell policy -> test plan
can-i to-policy [--config checks.yaml]          # checks.yaml -> OpenShell policy
```

`CAN_I_CONFIG` overrides the default config path everywhere.

### OpenShell policy interop (both directions)

**Policy → test plan.** Point `can-i` at an OpenShell policy and it derives the probes that
verify exactly what that policy is supposed to enforce — grants become `expect: allowed`
checks, and implied default-deny behavior becomes `expect: denied` control probes:

```bash
can-i from-policy examples/openshell-policy.yaml -o checks-derived.yaml
can-i run --config checks-derived.yaml      # reports "expectations: N/M met"
can-i web --config checks-derived.yaml      # Expected column shows ✓/✗ live
```

**Test plan → policy.** Build a `checks.yaml` describing what your workload needs, then
export it as a usable OpenShell policy. Checks expecting `allowed` (or with no expectation)
become grants; checks expecting `denied` are omitted — in OpenShell, absence *is* the denial:

```bash
can-i to-policy --config checks.yaml > openshell-policy.yaml
```

The same interop is in the web editor: **⇪ Import OpenShell policy** loads a policy file into
the editor as checks (Save & Apply to activate), **⇩ Export as policy** downloads the current
checks as a policy document.

### Web API

| Endpoint | Description |
|---|---|
| `GET /` | dashboard SPA |
| `GET /api/system` | hostname, kernel, uid/user, effective capabilities, sandbox status |
| `GET /api/probes` | latest results for every check |
| `POST /api/run` | trigger a probe run, returns results |
| `GET /api/config` | raw `checks.yaml` |
| `PUT /api/config` | validate + persist + hot-reload `checks.yaml` (422 on schema errors) |
| `POST /api/policy/import` | OpenShell policy YAML → generated checks.yaml text |
| `GET /api/policy/export` | current checks → OpenShell policy YAML |
| `GET /api/events` | SSE stream: `snapshot`, `run_start`, `probe`, `run_complete`, `config` |

### checks.yaml schema

```yaml
version: 1
settings:
  interval_seconds: 3          # continuous-mode loop
  default_timeout_seconds: 5
checks:
  - id: fs-read-shadow         # unique
    name: Read /etc/shadow
    category: filesystem       # network | filesystem | process
    command: head -c1 /etc/shadow
    timeout: 2                 # optional, falls back to the default
    description: ...           # optional
    expect: denied             # optional: allowed|denied — results report ✓/✗ met
```

Invalid YAML or schema violations are rejected with field-level errors and never overwrite the on-disk config.

## Testing

```bash
mise run test                 # unit + integration + API + headless TUI tests
mise run test:container       # e2e: builds the BCI image, asserts DENIED under --cap-drop ALL
```

The suite covers probe-engine timeout/exit-code/output handling, YAML schema validation, all REST endpoints, the SSE broadcast (against a real uvicorn socket), headless TUI keybindings and rendering, CLI end-to-end, and a containerized sandbox scenario.

## Repository layout

```
├── checks.yaml            # default probe definitions
├── .mise.toml             # runtimes + tasks (install/test/dev/start:tui/start:web)
├── install.sh             # one-line bootstrap (mise-based)
├── Dockerfile             # multi-stage, SUSE BCI only
├── pyproject.toml
├── examples/openshell-policy.yaml   # sample policy for from-policy
├── src/cani/
│   ├── config.py          # checks.yaml load/validate/atomic save
│   ├── policy.py          # OpenShell policy <-> checks.yaml interop
│   ├── probes.py          # async probe engine (timeout, capture, truncation)
│   ├── state.py           # shared state + subscriber fan-out
│   ├── system_info.py     # host/sandbox introspection (caps, apparmor, seccomp)
│   ├── server.py          # FastAPI REST + SSE + dashboard
│   ├── tui.py             # Textual terminal UI
│   ├── cli.py             # `can-i` entry point
│   └── web/index.html     # dashboard SPA
└── tests/                 # unit, integration, TUI, CLI, container e2e
```

## License

MIT — see [LICENSE](LICENSE).
