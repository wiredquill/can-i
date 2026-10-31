# Enable Landlock on SUSE Linux Enterprise Server and openSUSE

OpenShell confines each sandbox's filesystem access with
[Landlock](https://docs.kernel.org/userspace-api/landlock.html), a Linux security module (LSM).
**Every node that runs OpenShell sandboxes needs Landlock switched on in its kernel**:
every Kubernetes node that can schedule a sandbox pod, and every Docker or Podman host running
a local gateway. OpenShell 0.1.5's Kubernetes runtime requires it even with
`landlock: best_effort`, so a node without Landlock starts no sandbox at all.

Do this before installing the OpenShell apps, and on each new node before it joins the cluster.

## How it works

SUSE kernels have Landlock built in (`CONFIG_SECURITY_LANDLOCK=y`), but on some releases it
is not in the list of LSMs the kernel activates at boot. That list is
`/sys/kernel/security/lsm`. To activate Landlock, add it to the `lsm=` kernel boot
parameter and reboot.

Tested on openSUSE Leap 15.6 (kernel 6.4) as an RKE2 node. The steps are the same on
SLES 15 SP6, which runs the same kernel line. On SLES 16, Leap 16.0 and Tumbleweed, check
first: Landlock may already be active.

## 1. Check the node

Run these on the node as root:

```bash
cat /sys/kernel/security/lsm
# lockdown,capability,apparmor,bpf            <- no landlock: continue with step 2
# lockdown,capability,landlock,apparmor,bpf   <- already on: you're done

grep CONFIG_SECURITY_LANDLOCK /boot/config-$(uname -r)
# CONFIG_SECURITY_LANDLOCK=y                  <- built in, can be switched on
```

If `CONFIG_SECURITY_LANDLOCK` is missing or `is not set`, the kernel can't run Landlock
(it needs kernel 5.13 or newer). Update the kernel first.

## 2. Add Landlock to the kernel command line

**Start from the current list and add `landlock` to it.** Don't write a new list from
scratch: `lsm=` replaces the kernel's default, so any module you leave out is switched off.
Leave out `apparmor` (SLES 15, Leap 15) or `selinux` (SLES 16, Leap 16) and you disable the
confinement that containerd, RKE2 and the rest of the host rely on. Landlock can go
anywhere in the list.

Find out which bootloader the node uses:

```bash
grep LOADER_TYPE /etc/sysconfig/bootloader
```

### GRUB 2 (`grub2` or `grub2-efi`), the usual case

```bash
cur=$(cat /sys/kernel/security/lsm)                  # e.g. lockdown,capability,apparmor,bpf
cp /etc/default/grub /etc/default/grub.bak
sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"/&lsm=landlock,${cur} /" /etc/default/grub
grep ^GRUB_CMDLINE_LINUX_DEFAULT /etc/default/grub   # check: lsm=landlock,lockdown,... is there
update-bootloader --refresh                          # or: grub2-mkconfig -o /boot/grub2/grub.cfg
```

If `GRUB_CMDLINE_LINUX_DEFAULT` already has an `lsm=` entry, edit that entry by hand
instead of running the `sed`. A kernel command line with two `lsm=` entries is confusing to
read later. An old `security=apparmor` entry can stay; `lsm=` takes precedence over it.

YaST works too: **System → Boot Loader → Kernel Parameters**, append
`lsm=landlock,<current list>` to **Optional Kernel Command Line Parameter**, then **OK**.

### systemd-boot or GRUB 2 with BLS (`systemd-boot`, `grub2-bls`)

These read the kernel options from `/etc/kernel/cmdline`:

```bash
cur=$(cat /sys/kernel/security/lsm)
cp /etc/kernel/cmdline /etc/kernel/cmdline.bak
sed -i "1s/\$/ lsm=landlock,${cur}/" /etc/kernel/cmdline
cat /etc/kernel/cmdline
sdbootutil update-all-entries
```

### Transactional systems (SL Micro, Leap Micro, MicroOS)

The root filesystem is read-only, but `/etc` is writable. Edit `/etc/default/grub` as in the
GRUB 2 section above, then regenerate the boot configuration in a new snapshot instead of
running `update-bootloader`:

```bash
transactional-update grub.cfg
```

### Other bootloaders

`cat /proc/cmdline` shows the options the node booted with. Put the `lsm=...` entry wherever
that bootloader keeps its kernel arguments.

## 3. Reboot

```bash
reboot
```

On Kubernetes, drain the node first so workloads move elsewhere, and uncordon it afterwards:

```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
# reboot the node, wait for it to come back Ready
kubectl uncordon <node>
```

On a single-node cluster everything is down until the node is back.

## 4. Check that it works

### On the node

```bash
cat /sys/kernel/security/lsm     # now includes landlock
cat /proc/cmdline                # shows your lsm=... entry
dmesg | grep -i landlock         # landlock: Up and running.
```

`Up and running.` means the kernel activated Landlock. If the list still has no `landlock`,
the node didn't boot with the new command line: check `/proc/cmdline` and the bootloader
step.

### Every node in a Kubernetes cluster, without SSH

`kubectl debug node` starts a pod on each node with the host's filesystem at `/host`:

```bash
for n in $(kubectl get nodes -o name); do
  printf '%s: ' "${n#node/}"
  kubectl debug "$n" -q -it=false --profile=general \
    --image=registry.suse.com/bci/bci-base:16.0 -- cat /host/sys/kernel/security/lsm
done
sleep 10
for p in $(kubectl get pods -o name | grep node-debugger-); do
  kubectl logs "$p"; kubectl delete "$p" --wait=false >/dev/null
done
```

The debug pods go into the current namespace. Each log line should include `landlock`.

### From inside an OpenShell sandbox

can-i reports the Landlock ABI version the kernel offers. Connect to a running can-i
sandbox and look at the system panel:

```bash
openshell sandbox connect can-i      # Kubernetes: kubectl -n <ns> exec -it deploy/can-i-client -- openshell sandbox connect can-i-sandbox
can-i run                            # header line shows "landlock: ABI vN"
```

`ABI v1` or higher means Landlock is active. `unsupported` means the node's kernel doesn't
have it. On the dashboard, a write to `/var/tmp` should be **DENIED** with **Blocked by:
Landlock**: `/var/tmp` is world-writable, so only Landlock can refuse it.

## Undo

Restore the backup and rebuild the boot configuration, then reboot:

```bash
cp /etc/default/grub.bak /etc/default/grub && update-bootloader --refresh   # GRUB 2
cp /etc/kernel/cmdline.bak /etc/kernel/cmdline && sdbootutil update-all-entries   # BLS
```
