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

### Who said no? (`blocked_by`)

Every denied probe carries a best-guess **blocker** — which layer refused it — derived from
stderr, the exit code and the sandbox context ([`attribution.py`](src/cani/attribution.py)):

| Kind | Layers | Meaning |
|---|---|---|
| **sandbox** (pink ◆) | `Landlock`, `seccomp`, `OpenShell egress`, `CAP_NET_RAW`, `no_new_privs`, `dropped`, … | the sandbox policy enforced this denial |
| **baseline** (grey ·) | `file perms`, `not installed`, `sudoers`, `missing`, … | would be denied without any sandbox |

The kernel doesn't say which LSM produced an errno, so this is a heuristic — e.g. EACCES on a
file whose unix mode bits *allow* access is attributed to Landlock inside OpenShell, while
`/etc/shadow` (mode 0640) is attributed to file permissions. The reason text in the detail
panel says what evidence each guess rests on.

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

# the sandbox does NOT publish :8000 — forward it out, then browse to
# http://<host-running-this-cli>:8477 (not :8000)
openshell forward service --target-port 8000 --local 0.0.0.0:8477 can-i

# or attach a terminal and drive the TUI yourself — the trailing command is
# required: without it OpenShell drops you into a shell, not the image entrypoint
openshell sandbox create --name can-i-tui \
  --from ghcr.io/wiredquill/can-i:latest \
  --policy examples/policy-to-run-can-i.yaml \
  -- can-i tui
```

[`examples/policy-to-run-can-i.yaml`](examples/policy-to-run-can-i.yaml) is the minimal grant
set: `/tmp` writable, `run_as_user: "10001"` (the image's `cani` user — `sandbox` only works
if the image ships that account), and egress only for `curl → suse.com:443`. It also needs
`read_only` on `/usr`, `/usr/local`, `/lib`, `/lib64`, `/etc` — the OpenShell baseline paths
do not cover `/usr/local`, where the image installs can-i.
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
filesystem, not port publishing. That's why the `openshell forward service` step above is
required; the dashboard on :8477 is served from INSIDE the sandbox. If it still doesn't load,
check the host firewall allows :8477 and that the web workload is up
(`openshell sandbox connect can-i`, then `curl -s localhost:8000 | head`).

Verified on OpenShell v0.1.5 (docker driver): `can-i run` in the sandbox reports
**12/12 expectations met** — suse.com/DNS and /tmp allowed; metadata denied (OpenShell egress);
unshare denied (seccomp: filter). /etc/shadow, /etc writes and /proc/1/environ are denied too,
but by unix file permissions (the workload runs as uid 10001) before Landlock is consulted, and
ping/nmap/sudo are simply not installed in the image — the "Blocked by" column shows this.
To see Landlock itself refuse, probe a path the mode bits allow but the policy omits
(e.g. `ls /var/lib`, `touch /var/tmp/x`).

### 4. Kubernetes with Rancher or SUSE AI Factory

The same demo on a cluster: the OpenShell gateway runs in Kubernetes, and can-i runs as one
of its sandboxes. Three Helm charts, each with a Rancher install form (`questions.yaml`):

| Chart | What it installs |
|---|---|
| [`charts/agent-sandbox`](charts/agent-sandbox) 1.0.10 | **OpenShell 1 - Agent Sandbox controller**: the `Sandbox` CRD + controller OpenShell's Kubernetes driver needs. Install first, **once per cluster** |
| [`charts/openshell-gateway`](charts/openshell-gateway) 0.1.8 | **OpenShell 2 - Gateway**: NVIDIA's OpenShell 0.1.5 chart (vendored, with a small [form patch](charts/openshell-gateway/nvidia-chart.patch)) and the [openshell-lab](https://github.com/wiredquill/openshell-lab) defaults. Creates no sandbox |
| [`charts/can-i`](charts/can-i) 0.1.2 | **OpenShell 3 - can-i sandbox**: a client pod (the `openshell` CLI) that creates the can-i sandbox and serves its dashboard |

The can-i form's **Sandbox image** dropdown picks the OpenShell child container:

| Choice | Image | Base |
|---|---|---|
| `suse` | `ghcr.io/wiredquill/can-i` | SUSE BCI Python 3.13 ([Dockerfile](Dockerfile)) |
| `opensuse` | `ghcr.io/wiredquill/can-i-opensuse` | openSUSE Leap 16.0 ([images/opensuse](images/opensuse/Dockerfile)) |
| `nvidia` | `ghcr.io/wiredquill/can-i-nvidia` | `nvcr.io/nvidia/base/ubuntu:24.04`, NVIDIA's default sandbox base ([images/nvidia](images/nvidia/Dockerfile)) |
| `custom` | any image with can-i and a uid-10001 user | |

**Publish the charts** to the `aif/charts` branch, which Rancher reads as a git ClusterRepo,
then register it once per Rancher management cluster:
```bash
scripts/publish-charts.sh --push
kubectl apply -f catalog/clusterrepo.yaml      # ClusterRepo wiredquill-can-i
```

**Install with Rancher Apps:** install *OpenShell 1 - Agent Sandbox controller* (once per
cluster), then *OpenShell 2 - Gateway*, then *OpenShell 3 - can-i sandbox* in the **same
namespace** as the gateway (can-i reads its `openshell-client-tls` Secret). The gateway alone
runs no sandbox; the can-i app creates it. The architecture diagram is in
[`charts/agent-sandbox/README.md`](charts/agent-sandbox/README.md#architecture).

**Or deploy the AI Factory blueprint**, which installs all three into one namespace:
```bash
kubectl apply -f catalog/blueprints/can-i-openshell-1-0-0.yaml
```
Then go to **AI Factory → Blueprints → can-i in an OpenShell sandbox → Deploy**.
[`catalog/blueprints/blueprint-catalog.yaml`](catalog/blueprints/blueprint-catalog.yaml)
lists it for the operator's catalog sync.

**Drive the demo** through the client pod, which also works as your `openshell` CLI:
```bash
kubectl -n <ns> exec -it deploy/can-i-client -- openshell sandbox connect can-i-sandbox   # then: can-i tui --stage
kubectl -n <ns> exec deploy/can-i-client -- openshell policy update can-i-sandbox \
  --rule-name can-i-egress --binary /usr/bin/curl \
  --add-endpoint opensuse.org:443:read-only:rest:enforce --wait
```
**Access from outside the cluster.** The dashboard is on the can-i Service (NodePort by
default; LoadBalancer or Ingress in the form). The gateway is ClusterIP by default, so the CLI
on your workstation goes through `kubectl port-forward svc/openshell 17680:8080` as in the
lab. For direct access, set its Service to NodePort or LoadBalancer and put the address you'll
dial in **Extra certificate names**: the CLI checks the gateway's certificate against it.
Sandboxes are never exposed; you reach them through the gateway.

Changing the image or starting policy in the form recreates the sandbox. The cluster needs
a CNI that enforces `NetworkPolicy`. If Agent Sandbox was already installed with
`kubectl apply`, skip its chart: Helm can't adopt those resources.

## Usage

```
can-i run  [--config checks.yaml] [--json]   # one-shot probe report (exit 0)
can-i tui  [--config checks.yaml]            # terminal UI (r/space run, a auto, b baseline, 1-3 show/hide category, 0 show all, tab next box, q quit)
can-i tui --stage                            # full-screen presenter view (toggle with s inside the TUI)
can-i web  [--config checks.yaml] [--host 0.0.0.0] [--port 8000]
can-i from-policy POLICY.yaml [-o checks.yaml]  # OpenShell policy -> test plan
can-i to-policy [--config checks.yaml]          # checks.yaml -> OpenShell policy
```

`CAN_I_CONFIG` overrides the default config path everywhere.

**Stage mode** (`can-i tui --stage`, or `s` in the TUI) is built for presenting from a plain
terminal — e.g. `kubectl exec -it <pod> -- can-i tui --stage` — with no browser or port-forward.
It shows one big tile per category with the agent's allowed actions, a banner announcing
**ACCESS GRANTED / REVOKED** as you change the policy, big totals vs. the pinned baseline (`b`),
and a trend line of allowed actions per run. The category switches (1-3, 0) work here too, and
on short terminals the totals and trend fold away so the tiles keep their room.

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
├── Dockerfile             # multi-stage, SUSE BCI (the default image)
├── images/                # opensuse/ and nvidia/ variants, client/ (openshell CLI pod)
├── charts/                # agent-sandbox, openshell-gateway, can-i (Rancher forms)
├── catalog/               # SUSE AI Factory blueprint + Rancher ClusterRepo
├── scripts/publish-charts.sh  # package charts onto the aif/charts branch
├── scripts/vendor-openshell-chart.sh  # re-vendor NVIDIA's chart + patch
├── pyproject.toml
├── examples/openshell-policy.yaml   # sample policy for from-policy
├── src/cani/
│   ├── config.py          # checks.yaml load/validate/atomic save
│   ├── policy.py          # OpenShell policy <-> checks.yaml interop
│   ├── probes.py          # async probe engine (timeout, capture, truncation)
│   ├── attribution.py     # which layer denied a probe (Landlock, seccomp, …)
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
