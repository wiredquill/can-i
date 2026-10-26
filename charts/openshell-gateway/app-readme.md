# OpenShell gateway

[NVIDIA OpenShell](https://docs.nvidia.com/openshell/latest/) 0.1.5 is the sandbox runtime
for autonomous agents. The gateway is its control plane: it holds the policies and
providers, and its Kubernetes driver runs each sandbox as a pod with a supervisor that
enforces network policy (L4 + L7), Landlock and seccomp.

This app is NVIDIA's official chart with the openshell-lab defaults and an install form.

**Before you install**
- The **Agent Sandbox controller** app (or `kubectl apply` of its `sandbox.yaml`) on this cluster.
- A CNI that enforces `NetworkPolicy` (RKE2's Canal/Calico/Cilium do).
- To deploy **can-i** with its own app, install it into this same namespace: it reads the
  `openshell-client-tls` Secret this chart creates.

**Using the CLI from your workstation**
```bash
kubectl -n <namespace> port-forward svc/openshell 17680:8080
```
Then register the gateway as `openshell-lab/k8s/setup.sh` does, with the client bundle from
the `openshell-client-tls` Secret.
