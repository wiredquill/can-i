# OpenShell 3 - can-i sandbox

What can the agent do?

[can-i](https://github.com/wiredquill/can-i) runs a set of harmless probes (network egress,
sensitive files, spawning processes, privilege escalation) and shows live which ones the
sandbox **allows** and which it **denies**. Here it runs as the workload of an
**OpenShell sandbox**, so the dashboard shows exactly what the OpenShell policy lets an
agent do. Grant access with `openshell policy update` and watch it flip.

Pick the sandbox image in the form: **SUSE BCI**, **openSUSE Leap 16** or **NVIDIA
Ubuntu**. Same probes and policy, different base image.

**Before you install**
- **OpenShell 1 - Agent Sandbox controller** (once per cluster) and **OpenShell 2 - Gateway**.
- Install can-i in the **same namespace** as the gateway. It reads the gateway's
  `openshell-client-tls` Secret.

**What it creates:** one small client pod (the `openshell` CLI). It creates the sandbox
through the gateway, forwards the dashboard to a Service (`svc/can-i`, NodePort by default:
open `http://<node-ip>:<nodePort>`), and deletes the sandbox when you
uninstall. The pod also works as your CLI:
```bash
kubectl exec -it deploy/can-i -- openshell sandbox connect can-i   # then: can-i tui --stage
kubectl exec deploy/can-i -- openshell policy get can-i --full
```
