# Logs and diagnostics

Find the interesting lines of a customer-case reproduction fast: the logs of a VM or of a cluster's pods, filtered on
the server by level, regex and a built-in set of **known problems** (OOM killer, segfaults, I/O errors, link down,
firmware, SELinux AVC, x509, image pulls, CrashLoopBackOff, DNS, connection refused / timeouts, NotReady nodes,
SR-IOV / VFIO / IOMMU, cloud-init, failed units…), each with a category and a plain-words hint. Plus an inventory of a
VM (OS, firmware versions, PCI, NIC drivers, disks, SR-IOV) for the "which firmware / driver?" questions.

Same code path for every caller: the UI, the REST API and the MCP tools (`vm_logs`, `vm_diagnostics`, `find_pods`,
`pod_logs`, see [mcp.md](mcp.md)).

## Where

- **VM**: Virtual machines → expand a VM → *Logs* / *Diagnostics* (page `/vms/<id>/logs`).
- **Cluster**: cluster page → *Logs* tab (k3s, kubeadm, OpenShift).

## VM logs

Read through the QEMU guest agent (`guest-exec`): no SSH key, no route to the VM, works on isolated lab networks.

| Source | What runs in the guest | Options |
|---|---|---|
| `journal` | `journalctl -o json -n <lines>` (the line level is the journal's priority) | `unit` (several), `priority`, `since` / `until` (`-1h`, `today`, `2026-10-11 10:00`), `boot` (0 current, -1 previous, `all`), `grep` (journalctl `--grep`, in the guest) |
| `dmesg` | `dmesg -T -x` (level from the kernel); `journalctl -k` for a previous boot or a time window | `boot`, `since` |
| `cloud-init` | `cloud-init status --long` + tail of `cloud-init-output.log` and `cloud-init.log` | |
| `file` | `tail -n <lines> -- <path>` | `path` (absolute) |

`lines` = how many lines are read (tail, max 20000).

**EL VMs** (RHEL / Alma / Rocky cloud images): their qemu-ga forbids `guest-exec` unless the VM is a cluster node or a
lab router (vm-manager unlocks it there). Logs then answer with a plain-words error explaining how to allow it
(`/etc/sysconfig/qemu-ga` with empty `BLACKLIST_RPC` / `FILTER_RPC_ARGS` + `(typepermissive virt_qemu_ga_t)`), and
diagnostics fall back on what the agent answers by itself (OS, kernel, interfaces, file systems).

**OpenShift nodes** (RHCOS: its qemu-ga forbids `guest-exec` too): logs fall back on `oc adm node-logs <node>`
through the API server (no SSH key, no route to the node, no debug image: works on disconnected clusters). Journal
with units / boot / since / grep, Kernel = the journal's kernel transport, File = paths under `/var/log/` (`--path`);
no cloud-init (RHCOS uses Ignition). Diagnostics add the node's API view: OS image, kernel, kubelet / CRI-O versions,
capacity, conditions (not-Ready ones are findings).

Levels: an explicit level in the line wins (klog `E1011 12:00:00`, `level=warning`, JSON `"level":"info"`), then the
journal priority, then keywords: kubelet / CRI-O log everything at priority 6 through stdout.

## Pod logs

`GET /clusters/{id}/pods?match=sriov` finds pods whose `namespace/name` contains the text (or `namespace`, `selector`,
`problems_only` = unhealthy: not running, a container waiting / failed / not ready, or restarted in the last hour), with
their containers, restarts and problems (CrashLoopBackOff, ImagePullBackOff, OOMKilled, the last line of a termination
message).

`GET /clusters/{id}/logs` takes one pod (`namespace` + `pod`) or every pod matching `match` / `namespace` / `selector`
(up to 30): `kubectl logs --timestamps` per container (`container`, `previous`, `tail`, `since`), lines prefixed
`[namespace/pod/container]`. Container **status lines** and the pods' **events** are added (`events=false` to skip):
ImagePullBackOff, FailedScheduling or a failing probe never appear in a container log. kubectl runs where the MCP
`kubectl` tool runs it (`services/kube_exec.py`): on the first running control plane through the guest agent
(k3s / kubeadm), the app's `oc` on the host (OpenShift).

## Filters (every source)

| Parameter | Effect |
|---|---|
| `level` | `warning` = warnings and errors, `error` = errors only |
| `include` / `exclude` | regex (case-insensitive) a line must / must not match |
| `problems_only` | only lines matching a known-problem rule |
| `category` | comma-separated categories or rule ids (`memory,storage`, `oom-killer`) |
| `context` | lines before / after each kept line (0-20) |
| `max_entries` | cap on returned lines (the most recent are kept, `truncated: true`) |
| `format=text` | the kept lines as plain text (the UI's *Download*) |

The answer: `entries` (`n` = line number in what was read, `text`, `level`, `rules`, `before` / `after`), `levels`
(counts), `summary` (per category: count, rules with counts, hint; computed over every line read, so a category shows
up even when other filters hide its lines), `command` (what ran), `pods` / `notes` for pod logs.

Levels: the journal's priority, `dmesg -x`'s level, else guessed from the text (`error`, `fatal`, `panic`, klog `E1011`,
`level=error`…); a line matching a rule is raised to at least that rule's level.

## Known-problem rules

`backend/app/services/log_rules.py` (`GET /logs/rules`): per rule an `id`, a `category`, a `level`, a case-insensitive
`pattern` (Python regex, searched in each line), an optional `ignore` regex for known harmless lines (`ata3: SATA
link down` = an empty SATA port of a q35 VM, `regulatory.db` firmware) and a `hint` written for a support engineer.
To add one: append a dict, keep the pattern specific (word boundaries: `IOMMU.*fault` matched "Default domain"), run
`cd e2e && node logs.js`. The UI compiles the same patterns in JavaScript only to highlight the match; the server
decides.

Lines produced by vm-manager's own guest-exec calls (Debian's qemu-ga logs every command with its arguments) never
match a rule: the diagnostics script would otherwise flag itself.

## Diagnostics

`GET /vms/{id}/diagnostics`, one guest-exec (missing tools are listed, not fatal): `/etc/os-release`, `uname`, kernel
command line, CPU / memory, `dmidecode -t bios|system` (else `/sys/class/dmi/id`: BIOS vendor / version / date, system
product, machine type), `lspci -nnk`, NICs (`ip -j addr` + `ethtool -i`: driver, version, firmware-version, bus-info;
VFs with MAC / VLAN / trust / spoofchk from `ip -d link`), `lsblk -J`, `df`, SR-IOV PFs (total / configured VFs,
driver), IOMMU group count, devices bound to vfio-pci, failed systemd units, the last 30 `journalctl -p err -b` lines,
and `findings` in plain words (failed units, links down, file systems over 90 %, SR-IOV without IOMMU, known problems
among the recent errors).

## Access

Logs can hold secrets (passwords on command lines, tokens): `/vms/{id}/logs` and `/clusters/{id}/logs` are admin-only
reads (`SECRET_READS_RE`), and so is the `file` source (any path). Diagnostics and the pod list stay readable by
viewers (an inventory and pod states), without the raw recent-error lines.

## Test

`cd e2e && node logs.js` (Debian VM with an igb NIC: provoked problems, every source and filter, diagnostics, MCP, UI
screenshots). `EL_IMAGE=almalinux-9` adds the EL guest-exec / fallback checks, `CLUSTER=<name>` the pod logs (namespace
`e2e-logs` with a crash-looping pod and an image that can't be pulled, deleted at the end).
