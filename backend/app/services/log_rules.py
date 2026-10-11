"""Built-in "known problems" ruleset for the log viewer (docs/logs.md).

A rule = id, category (groups rules in the summary), level (error | warning), regex (case-insensitive, searched in
each line), optional ignore regex (known harmless lines) and a plain-words hint for a support engineer. Add a rule
here: the API,
the UI and the MCP tools pick it up (GET /logs/rules lists them).
"""
from typing import Dict, List

CATEGORIES: Dict[str, str] = {
    "memory": "Memory / OOM",
    "crash": "Crashes",
    "storage": "Storage / I/O",
    "network": "Network links",
    "firmware": "Firmware / microcode",
    "selinux": "SELinux",
    "tls": "Certificates / TLS",
    "images": "Image pulls",
    "pods": "Pods / containers",
    "dns": "DNS",
    "connectivity": "Connections",
    "kubelet": "Kubelet / nodes",
    "sriov": "SR-IOV / VFIO / IOMMU",
    "cloud-init": "cloud-init",
    "systemd": "systemd units",
    "kernel": "Kernel",
}

RULES: List[Dict[str, str]] = [
    # memory
    {"id": "oom-killer", "category": "memory", "level": "error",
     "pattern": r"out of memory|oom-kill|invoked oom-killer|oom_reaper|Memory cgroup out of memory|OOMKilled",
     "hint": "A process was killed for lack of memory: give the VM / container more RAM or lower the workload "
             "(the line names the victim; for pods check the memory limit)."},
    {"id": "page-alloc-failure", "category": "memory", "level": "warning",
     "pattern": r"page allocation failure|cannot allocate memory|ENOMEM",
     "hint": "The kernel or a program could not allocate memory (fragmentation or real shortage)."},
    # crashes
    {"id": "segfault", "category": "crash", "level": "error",
     "pattern": r"segfault at|segmentation fault|general protection fault|core dumped|SIGSEGV",
     "hint": "A program crashed (bad memory access). Note the binary and library in the line; coredumpctl lists dumps."},
    {"id": "kernel-oops", "category": "kernel", "level": "error",
     "pattern": r"kernel panic|Oops:|BUG: |kernel BUG at|Call Trace:|soft lockup|hard LOCKUP|hung_task|"
                r"blocked for more than \d+ seconds|RIP: ",
     "hint": "Kernel trouble (oops / lockup / hung task): read the lines after it for the module and call trace."},
    {"id": "panic-go", "category": "crash", "level": "error",
     "pattern": r"^.{0,80}\bpanic: |goroutine \d+ \[running\]|Traceback \(most recent call last\)|"
                r"Exception in thread|FATAL ERROR|fatal error:",
     "hint": "An application crashed with a stack trace (Go panic / Python traceback / Java exception): the first "
             "line of the trace is the cause."},
    # storage
    {"id": "io-error", "category": "storage", "level": "error",
     "pattern": r"I/O error|blk_update_request|Buffer I/O error|critical medium error|end_request: I/O|"
                r"EXT4-fs error|XFS .*(corruption|Internal error|metadata I/O error)|Remounting filesystem read-only|"
                r"read-only file system",
     "hint": "Disk I/O failure or file system corruption: check the backing volume / pool space on the host and "
             "the disk in the VM (smartctl, xfs_repair / fsck when unmounted)."},
    {"id": "disk-full", "category": "storage", "level": "error",
     "pattern": r"No space left on device|disk quota exceeded|DiskPressure|ephemeral-storage|"
                r"evicted.*(disk|storage)|ENOSPC",
     "hint": "A file system is full: df -h in the guest; for Kubernetes nodes DiskPressure evicts pods."},
    {"id": "scsi-timeout", "category": "storage", "level": "warning",
     "pattern": r"task abort|timing out command|SCSI error|ata\d+.*(failed|exception|hard resetting)|nvme.*timeout",
     "hint": "Storage commands time out: the host storage is slow or overloaded (I/O wait), or the device is failing."},
    # network
    {"id": "link-down", "category": "network", "level": "warning",
     "pattern": r"\blink (is )?down\b|NIC Link is Down|carrier lost|Link is Down|entered disabled state|"
                r"tx timeout|transmit (queue \d+ )?timed out|NETDEV WATCHDOG",
     "ignore": r"SATA link down",  # empty SATA ports of q35 VMs
     "hint": "A network interface lost its link or its TX queue hung: check the libvirt network / bridge, the cable "
             "(physical hosts), or the NIC driver (a TX timeout is often a driver / firmware bug)."},
    {"id": "nm-failure", "category": "network", "level": "warning",
     "pattern": r"NetworkManager.*(failed|state change: .* -> failed)|dhcp4? .*(timeout|failed)|"
                r"No DHCPOFFERS? received|DHCP(v6)? .*(timed out|no lease)",
     "hint": "The guest did not get a DHCP lease or a connection failed to activate: is the network's DHCP (or the "
             "group router's dnsmasq) up, and does the MAC have a reservation?"},
    {"id": "mtu", "category": "network", "level": "warning",
     "pattern": r"frag(mentation)? needed|packet too big|message too long|EMSGSIZE|mtu .*(exceed|mismatch)",
     "hint": "MTU problem: packets bigger than the path allows (overlay networks need a smaller pod MTU; see "
             "docs/router-cases.md for PMTUD black holes)."},
    # firmware
    {"id": "firmware", "category": "firmware", "level": "warning",
     "pattern": r"firmware.*(fail|error|bug|missing|not found|timed out)|Direct firmware load .* failed|"
                r"\[Firmware Bug\]|microcode.*(error|fail|update.*fail)|ACPI (Error|BIOS Error)|"
                r"failed to load (firmware|fw)",
     "ignore": r"regulatory\.db",  # wireless regulatory database: harmless without Wi-Fi
     "hint": "Firmware / BIOS / microcode problem: compare the firmware versions (Diagnostics tab: dmidecode, "
             "ethtool -i firmware-version) with the vendor's supported matrix; missing firmware = linux-firmware pkg."},
    {"id": "mce", "category": "firmware", "level": "error",
     "pattern": r"Machine check|mce: \[Hardware Error\]|Hardware Error|EDAC .*(error|CE|UE)",
     "hint": "Hardware error reported by the CPU / memory controller (on physical hosts: failing DIMM / CPU)."},
    # SELinux
    {"id": "selinux-avc", "category": "selinux", "level": "warning",
     "pattern": r"avc:\s+denied|SELinux is preventing|type=AVC",
     "hint": "SELinux denied an access: `ausearch -m avc -ts recent` / `sealert -a /var/log/audit/audit.log`; fix the "
             "file context (restorecon) or the boolean rather than disabling SELinux."},
    # TLS
    {"id": "x509", "category": "tls", "level": "error",
     "pattern": r"x509:|certificate (has )?expired|certificate is not (yet )?valid|unknown authority|"
                r"certificate signed by unknown|tls: (bad certificate|failed to verify)|SSL_ERROR|"
                r"certificate verify failed|self[- ]signed certificate",
     "hint": "TLS trust problem: expired / not-yet-valid certificate (check the clocks: chrony), or a CA the client "
             "doesn't trust (mirror registry CA, proxy CA: add it to the trust bundle)."},
    # image pulls
    {"id": "image-pull", "category": "images", "level": "error",
     "pattern": r"ImagePullBackOff|ErrImagePull|ErrImageNeverPull|Failed to pull image|failed to resolve reference|"
                r"manifest unknown|pull access denied|unauthorized: authentication required|"
                r"toomanyrequests|InvalidImageName",
     "hint": "Image pull failure: wrong name/tag, missing pull secret, registry unreachable, or (disconnected labs) "
             "the image isn't mirrored (see docs/disconnected.md)."},
    # pods
    {"id": "crashloop", "category": "pods", "level": "error",
     "pattern": r"CrashLoopBackOff|Back-off restarting failed container|Error: container .* exited|"
                r"CreateContainerConfigError|CreateContainerError|RunContainerError",
     "hint": "A container keeps exiting: read its previous logs (previous = true) and check its exit code / probes."},
    {"id": "pod-unschedulable", "category": "pods", "level": "warning",
     "pattern": r"FailedScheduling|Insufficient (cpu|memory)|didn't match Pod's node affinity|untolerated taint|"
                r"FailedMount|FailedAttachVolume|Evicted",
     "hint": "The pod can't be scheduled or its volume can't mount: not enough resources, taints/affinity, or "
             "storage (no default StorageClass, PVC pending)."},
    {"id": "probe-failed", "category": "pods", "level": "warning",
     "pattern": r"(Liveness|Readiness|Startup) probe failed|probe errored",
     "hint": "Health probe failing: the app is slow to start (raise initialDelay / startupProbe) or really unhealthy."},
    # DNS
    {"id": "dns", "category": "dns", "level": "error",
     "pattern": r"no such host|NXDOMAIN|SERVFAIL|Temporary failure in name resolution|Name or service not known|"
                r"could not resolve|server misbehaving|lookup .* on .*: (read|dial)|i/o timeout.*:53|"
                r"Could not resolve host",
     "hint": "Name resolution failed: check /etc/resolv.conf, the group router's dnsmasq (split DNS zones), and "
             "for clusters the api / *.apps records."},
    # connectivity
    {"id": "conn-refused", "category": "connectivity", "level": "error",
     "pattern": r"connection refused|ECONNREFUSED|connect: no route to host|network is unreachable|"
                r"EHOSTUNREACH|ENETUNREACH",
     "hint": "Nothing listens on the target port, or no route: is the service up, the right IP/port, a firewall "
             "(nft on the router, firewalld) or egress blocked?"},
    {"id": "timeout", "category": "connectivity", "level": "warning",
     "pattern": r"(connection|i/o|dial tcp.*|context deadline|read|handshake) (timed out|timeout)|"
                r"deadline exceeded|ETIMEDOUT|Connection timed out|TLS handshake timeout",
     "hint": "Connection timed out: packets dropped silently (firewall drop, egress blocked, MTU black hole, wrong "
             "route) or a very slow peer."},
    {"id": "conn-reset", "category": "connectivity", "level": "warning",
     "pattern": r"connection reset by peer|ECONNRESET|broken pipe|unexpected EOF",
     "hint": "The peer closed the connection abruptly (crash, proxy, load balancer, MTU)."},
    # kubelet / nodes
    {"id": "node-notready", "category": "kubelet", "level": "error",
     "pattern": r"NotReady|node .* not ready|NodeNotReady|Kubelet stopped posting node status|"
                r"container runtime (network )?not ready|NetworkPluginNotReady|cni plugin not initialized|"
                r"PLEG is not healthy",
     "hint": "Node not ready: kubelet can't reach the API, the container runtime is down, or the CNI (Flannel / "
             "OVN) isn't ready. journalctl -u kubelet on the node."},
    {"id": "kubelet-error", "category": "kubelet", "level": "warning",
     "pattern": r"kubelet.*(failed|error)|Failed to (start|create|kill) (container|pod sandbox)|"
                r"failed to get sandbox|FailedCreatePodSandBox",
     "hint": "kubelet / CRI error creating or killing a container / pod sandbox (often CNI or runtime trouble)."},
    {"id": "etcd", "category": "kubelet", "level": "warning",
     "pattern": r"etcdserver: (request timed out|leader changed|too many requests)|apply request took too long|"
                r"slow fdatasync|etcd .*(unhealthy|lost leader)",
     "hint": "etcd is slow or lost its leader: the node disk is too slow or the VM is starved of CPU."},
    # SR-IOV
    {"id": "sriov", "category": "sriov", "level": "error",
     "pattern": r"sriov.*(error|fail|cannot|unable)|config-?daemon.*(error|fail)|SriovNetworkNodeState.*fail|"
                r"syncStatus.*Failed|failed to (configure|create|set) .*VFs?|sriov_numvfs|"
                r"vf \d+ .*(fail|error)|VF .*(fail|error|not found)|no VFs?|PF .*not found",
     "hint": "SR-IOV configuration error: numVfs / VF options not applied (driver must support SR-IOV, the PF must "
             "be up, enough VFs: see docs/sriov.md; the config daemon log on the node has the detail)."},
    {"id": "vfio-iommu", "category": "sriov", "level": "error",
     "pattern": r"vfio.*(error|fail|not viable|No such device)|group \d+ is not viable|"
                r"IOMMU\b.*\b(not (found|enabled|available)|faults?|error)\b|DMAR:.*\b(fault|error)\b|"
                r"iommu=?.* not enabled|No IOMMU|AMD-Vi:.*(fault|error)",
     "hint": "VFIO / IOMMU problem: the IOMMU must be on (intel_iommu=on / vIOMMU on the VM), the device's IOMMU "
             "group must hold only devices bound to vfio-pci."},
    # cloud-init
    {"id": "cloud-init", "category": "cloud-init", "level": "error",
     "pattern": r"cloud-init.*(error|fail|traceback)|util.py\[WARNING\]|\[ERROR\]|"
                r"status: (error|degraded)|Failed running /var/lib/cloud|"
                r"(module|config-)\S+ .*failed|Unhandled non-multipart",
     "hint": "cloud-init failed a module: the user-data step named in the line didn't run (bad YAML, package not "
             "found, no network at first boot). `cloud-init status --long` and cloud-init-output.log tell which."},
    # systemd
    {"id": "unit-failed", "category": "systemd", "level": "error",
     "pattern": r"Failed to start |entered failed state|Failed with result|Main process exited, code=(exited|killed|dumped), "
                r"status=[1-9]|Start request repeated too quickly|Dependency failed for",
     "hint": "A systemd unit failed: `systemctl status <unit>` and `journalctl -u <unit> -b` (filter with unit= on "
             "the journal source)."},
]
