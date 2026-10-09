# SR-IOV labs with VM Manager

Two ways to give VMs SR-IOV NICs:

| | Emulated (any host, e.g. a gaming rig without IOMMU) | Real hardware (RHEL lab host with an SR-IOV NIC) |
|---|---|---|
| What the VM gets | an **igb** NIC: QEMU's Intel 82576 (PF `8086:10c9`), up to **7 VFs** created *inside* the guest (`8086:10ca`, driver `igbvf`) | a **VF of the host NIC** passed through (PCI, vfio) from an "SR-IOV VF pool" network |
| Host needs | QEMU ≥ 8.0 (igb model), libvirt that knows `igb` | IOMMU on (`intel_iommu=on iommu=pt`, VT-d in firmware), a PF with `sriov_totalvfs` > 0: see the checklist in §3 |
| Good for | SR-IOV operator / device plugin / CNI behaviour, netdevice + vfio (DPDK) modes, failure cases | performance, real driver/firmware behaviour (mlx5, ice, i40e…) |

## 1. Emulated SR-IOV: igb NIC + virtual IOMMU

Create the VM (UI: *Create VM → More network interfaces, SR-IOV*; or API / OpenTofu):

```bash
curl -X POST localhost:8000/api/v1/vms -H 'content-type: application/json' -d '{
  "name": "sriov1", "memory": 4096, "vcpu": 2, "cloud_image_id": 1, "cloudinit_username": "admin",
  "cloudinit_ssh_keys": ["ssh-ed25519 …"],
  "extra_nics": [{"network": "sriov-net", "model": "igb"}],
  "iommu": true, "guest_kernel_args": "intel_iommu=on iommu=pt", "start": true}'
```

- `extra_nics` / `POST /vms/{id}/nics {network, model: "igb"}` adds the PF (hot-plug works on running VMs).
- `iommu: true` / `PUT /vms/{id}/iommu {enabled}` adds `<iommu model='intel'><driver intremap='on' caching_mode='on' iotlb='on'/></iommu>`
  and `<features><ioapic driver='qemu'/>` (interrupt remapping needs QEMU's split irqchip; caching mode is what
  lets the guest give devices to vfio). q35 only. It is a machine-level change: it applies at the **next cold
  start** (power off + start), not on a reboot from inside the guest.
- `guest_kernel_args` (cloud images): cloud-init adds them with `grubby` (EL) or a `/etc/default/grub.d` drop-in +
  `update-grub` (Debian/Ubuntu) and reboots the VM once. For other guests add `intel_iommu=on iommu=pt` yourself.
  Without the vIOMMU + these args, VFs still work as kernel netdevices, but vfio-pci can't bind (no IOMMU group).

In the guest:

```bash
PF=enp2s0                                   # the igb NIC (eth1 on EL images with net.ifnames=0)
sudo ip link set $PF up                     # VFs stay in reset while the PF is down
echo 4 | sudo tee /sys/class/net/$PF/device/sriov_numvfs     # max: cat …/sriov_totalvfs  (7)
lspci -nnk -d 8086:10ca                     # 4 x "82576 Virtual Function", driver igbvf
ip link show $PF                            # vf 0..3 with MAC / VLAN / spoofchk / trust
# vfio (DPDK, SR-IOV CNI vfio mode):
VF=$(basename $(readlink /sys/class/net/$PF/device/virtfn0))
sudo modprobe vfio-pci
echo vfio-pci | sudo tee /sys/bus/pci/devices/$VF/driver_override
echo $VF | sudo tee /sys/bus/pci/devices/$VF/driver/unbind
echo $VF | sudo tee /sys/bus/pci/drivers_probe     # lspci -k: vfio-pci, /dev/vfio/<group> appears
```

Verified (2026-10-05, host QEMU 11.1 / libvirt 12.8):

| Guest | PF driver | VF driver | vfio-pci bind | Notes |
|---|---|---|---|---|
| Debian 13 (6.12) | igb | igbvf | yes, `/dev/vfio/28` | DHCP over a VF; `dpdk-testpmd` (DPDK 24.11, `net_e1000_igb_vf`, IOMMU type 1) answered pings in icmpecho mode |
| AlmaLinux 10.2 (6.12 el10) | igb | igbvf | yes | igb, igbvf and vfio-pci are in `kernel-modules-core` (also what RHCOS ships) |
| Debian 13 as **L2** inside an AlmaLinux 10 L1 | — | igbvf | — | VF passed through by L1's libvirt from a VF pool network: see §3 |

On the PF, `ip link set $PF vf N mac|vlan|spoofchk|trust|max_tx_rate` are accepted (what sriov-cni calls);
`state enable|auto` (link-state) is not supported by the igb driver (same on a real 82576).

### Honest limits of the emulation

- Performance is that of an emulated NIC (all VF traffic goes through QEMU's device model and the host tap): fine
  for control-plane behaviour, useless for throughput/latency numbers.
- Only what QEMU's igb models: no RDMA, no switchdev / OVS hardware offload, no hardware timestamping guarantees,
  no firmware quirks of real NICs (mlx5, ice, i40e, …). VLAN / rate-limit / spoof settings are accepted by the
  driver; whether QEMU enforces each of them was not verified.
- Changing `sriov_numvfs` follows the kernel rule (go through 0). VFs vanish at guest reboot unless configured
  (the operator does it, or a udev rule, see §3).
- igb is an Intel 1 GbE 82576: an "old" NIC. Driver names, VF counts and operator behaviour differ from the
  customer's hardware; reproduce logic, not hardware bugs.

## 2. OpenShift / Kubernetes with these VMs

### SR-IOV Network Operator (OpenShift) and the 82576

The operator only manages NICs listed in its `supported-nic-ids` ConfigMap and, with its admission webhook,
rejects `SriovNetworkNodePolicy` objects selecting others. The config daemon also skips unsupported devices
during discovery (unless the operator runs in dev mode).

- The upstream chart (k8snetworkplumbingwg) lists the 82576: `Intel_ixgbe_82576: "8086 10c9 10ca"`.
- OpenShift's OLM manifest (`openshift/sriov-network-operator`, `manifests/stable/supported-nic-ids_v1_configmap.yaml`)
  did **not** list it when checked: on OpenShift, treat it as an unsupported NIC.

Official way to use an unsupported NIC on OpenShift (Red Hat docs "Configuring the SR-IOV Network Operator" and the
KCS "Configuring the SR-IOV Network Operator to use an unsupported NIC"): disable the operator webhook, and add
the NIC to the ConfigMap so the daemon discovers it:

```bash
oc patch sriovoperatorconfig default -n openshift-sriov-network-operator --type=merge \
  -p '{"spec":{"enableOperatorWebhook":false}}'
oc patch configmap supported-nic-ids -n openshift-sriov-network-operator --type=merge \
  -p '{"data":{"Intel_igb_82576":"8086 10c9 10ca"}}'
# then restart the config daemon (and webhook, if kept) pods so they reload the list
oc delete pod -n openshift-sriov-network-operator -l app=sriov-network-config-daemon
```

Unsupported NICs are not covered by Red Hat support: fine for a lab reproducing operator logic. For single-node
labs also set `disableDrain: true` in the `SriovOperatorConfig`.

Node prerequisites (OpenShift): the worker VMs need the igb NIC(s) and, for `deviceType: vfio-pci`, the vIOMMU plus
`intel_iommu=on iommu=pt` through a MachineConfig `kernelArguments` (the cloud-init option doesn't apply to RHCOS).

```yaml
apiVersion: sriovnetwork.openshift.io/v1
kind: SriovNetworkNodePolicy
metadata: {name: igb-netdev, namespace: openshift-sriov-network-operator}
spec:
  resourceName: igbnetdev
  nodeSelector: {feature.node.kubernetes.io/network-sriov.capable: "true"}
  numVfs: 4                    # <= 7
  nicSelector: {vendor: "8086", deviceID: "10c9", pfNames: ["enp2s0"]}
  deviceType: netdevice        # or vfio-pci (needs the vIOMMU + kernel args)
```

### Vanilla Kubernetes: sriov-network-device-plugin + sriov-cni

Create the VFs yourself (or with the upstream operator), then a device-plugin config selecting the VFs:

```json
{"resourceList": [
  {"resourceName": "igb_netdev", "selectors": {"vendors": ["8086"], "devices": ["10ca"], "drivers": ["igbvf"], "pfNames": ["enp2s0"]}},
  {"resourceName": "igb_vfio",   "selectors": {"vendors": ["8086"], "devices": ["10ca"], "drivers": ["vfio-pci"]}}
]}
```

`10c9` is the PF, `10ca` the VF device id; the device plugin selects VFs, so use `10ca`.

## 3. Real hardware (RHEL lab host): VF pool networks

A VF pool is a libvirt network `<forward mode='hostdev' managed='yes'><driver name='vfio'/><pf dev='ens1f0'/></forward>`
(+ `<vlan><tag id/></vlan>` when tagged): every VM NIC on it gets a free VF of the PF as a PCI device. No bridge, IP
or DHCP: the VF sits on the physical network the PF is cabled to.

### Checklist (the Host page → SR-IOV card checks all of this and prints the fix)

| # | What | How to check by hand | Fix |
|---|---|---|---|
| 1 | **VT-d / AMD-Vi in the firmware** (+ "SR-IOV Global Enable" on servers) | `ls /sys/firmware/acpi/tables/DMAR` (AMD: `IVRS`) | BIOS/UEFI setup |
| 2 | **IOMMU on in the kernel** | `ls /sys/kernel/iommu_groups \| wc -l` > 0 | EL: `sudo grubby --update-kernel=ALL --args="intel_iommu=on iommu=pt"` + reboot (Debian: `/etc/default/grub` + `update-grub`; Pop!_OS: `kernelstub -a`) — the card shows the command for the distro |
| 3 | `iommu=pt` (recommended, not required) | `grep iommu=pt /proc/cmdline` | same command |
| 4 | Interrupt remapping (Intel) | ecap bit 3 of `/sys/class/iommu/dmar*/intel-iommu/ecap` | firmware (x2APIC / IR); last resort `vfio_iommu_type1 allow_unsafe_interrupts=1` |
| 5 | `vfio-pci` available | `modinfo vfio-pci` | EL: `kernel-modules-core` |
| 6 | **PF firmware allows VFs** | `cat /sys/class/net/<pf>/device/sriov_totalvfs` > 0 | mlx5: `mstconfig -d <pci> set SRIOV_EN=1 NUM_OF_VFS=<n>` + reboot; i40e / ice: NVM update, ice needs its DDP package (else "safe mode", no SR-IOV) |
| 7 | **PF link up with carrier** | `ip link show <pf>` (`state UP`), `ethtool <pf>` | `ip link set <pf> up` (make it persistent with an NM connection with `ipv4.method disabled`); cable / switch port |
| 8 | **Each VF alone in its IOMMU group** (ACS) | `ls /sys/bus/pci/devices/<vf>/iommu_group/devices` | NIC in a CPU root-port slot, ACS / ARI in the firmware |
| 9 | Privileged helper version ≥ 3 | Host page | re-run `scripts/setup.sh` |

### Steps

1. **Host page → SR-IOV**: per PF, set the VF count (helper `sriov-set-numvfs`; refused while one of its VFs is
   passed through to a running VM). Options per PF, applied to every VF through `ip link set <pf> vf N …`:
   - **VF trust** (`trust on`): the guest may change its VF's MAC and use promiscuous / all-multicast. Needed for
     bonding inside the guest (active-backup moves MACs), OpenShift / OVN, VRRP / keepalived. Without it a guest's
     `ip link set <vf> address …` fails with "Cannot assign requested address".
   - **Spoof checking** (`spoofchk`, on by default): drops frames whose source MAC isn't the VF's. Turn it off with
     trust for bonding / failover MAC moves.
   - **Keep across reboots**: the helper (`sriov-persist`) stores the PF (by PCI address) with its VF count and options
     in `/etc/vm-manager/sriov.conf` and enables `vm-manager-sriov.service` (oneshot, before libvirt), which runs
     `helper sriov-restore` at boot (waits up to 90 s for the PF driver). Changing the count later updates the stored
     one. The last PF turned off disables and removes the unit. The PF row shows a "persistent" label.
   API: `PUT /api/v1/hosts/sriov/{pf} {num_vfs?, trust?, spoofchk?, persistent?}`. The PF's expandable row lists
   every VF: driver, MAC, VLAN, trust, spoof check (as the PF driver reports them), IOMMU group and what shares it.
2. **Networks → Create network → "SR-IOV VF pool"**: PF + optional **VLAN tag** (1–4094): the PF tags every VF of the
   pool on the wire, the guest sees untagged traffic (a port-based VLAN, what `<vlan><tag>` does for hostdev; trunks
   are not supported on VFs by libvirt).
3. **Add a NIC on that network** (VM details, `extra_nics`, or `vmmanager_nic`), optionally with its own **VLAN tag**
   (overrides the pool's). libvirt picks a free VF, binds it to vfio-pci (managed), sets the NIC's MAC and VLAN on the
   VF through the PF, and gives the VF back to its driver (MAC / VLAN restored) when the VM stops or the NIC is
   unplugged. Hot-plug and hot-unplug work.

Before a VM with VF NICs starts (or a VF is hot-plugged) the app checks the pool and says in plain words why it
can't work: no IOMMU (with the fix), the PF has 0 VFs, "No free VF in pool 'x': all 4 VFs of ens1f0 are already passed
through to running VMs", "Not enough VFs … this needs 3, ens1f0 has only 2", the VF's IOMMU group contains other
devices, or the PF disappeared (renamed NIC). If libvirt still fails, its message comes with a pointer to these checks.

**vfio-pci vs `<driver name='vfio'/>`**: the pool's `<driver name='vfio'/>` only says "pass through with VFIO" (the only
backend QEMU has); `managed='yes'` makes libvirt unbind the VF from its driver (iavf, ixgbevf, mlx5_core…) and bind it
to `vfio-pci` at VM start, then back. Don't bind pool VFs to vfio-pci yourself (driverctl, `driver_override`): they
would still work, but the app counts VFs bound to vfio-pci as "in use". The guest loads the VF driver itself (iavf,
ixgbevf, mlx5_core, igbvf): RHEL / RHCOS and Debian have them.

**Mellanox / NVIDIA (mlx5)**: VFs are bound to `mlx5_core` on the host until a VM takes them; the firmware caps
`sriov_totalvfs` (`mstconfig … NUM_OF_VFS`). VF pools need the eswitch in **legacy** mode (the default). switchdev mode
(OVS hardware offload, representors, `devlink dev eswitch set … mode switchdev`) is out of scope: libvirt hostdev pools
don't manage representors. On ConnectX VFs the VLAN set by the PF works as above; `trust on` is what lets OpenShift's
SR-IOV operator / bonding change MACs.

### How to verify on a real host

```bash
# 1. Host page: every host check green; set 4 VFs on the PF, trust on, spoof checking off, keep across reboots
cat /etc/vm-manager/sriov.conf; systemctl is-enabled vm-manager-sriov.service
ip link show ens1f0            # vf 0..3: trust on, spoof checking off
# 2. Pool with VLAN 100, a VM with a VF NIC
sudo virsh net-dumpxml vfpool-ens1f0     # <driver name='vfio'/>, <pf dev>, <vlan><tag id='100'/>
ip link show ens1f0            # the VM's MAC + "vlan 100" on one VF
# in the guest: lspci -nnk (iavf / ixgbevf / mlx5_core), DHCP or a static IP on VLAN 100 of the switch
# 3. reboot the host: VFs + trust/spoofchk come back (systemctl status vm-manager-sriov), the pool autostarts, the VM starts
# 4. e2e: BASE_URL=<app> HOST_SH="ssh labhost" PF=ens1f1 VM_NAME=<a running VM> node e2e/sriov-real.js
```

OpenTofu: `vmmanager_sriov_pf` (VF count, `trust`, `spoofchk`, `persistent`; destroy = 0 VFs, not persistent),
`vmmanager_network` `mode = "hostdev"` + `vlan`, `vmmanager_nic` `vlan`. Example: `examples/opentofu/sriov-pool`.

### Verified nested (2026-10-09)

L0 = this rig (no IOMMU); L1 = AlmaLinux 10.2 VM created by the app (vIOMMU, `intel_iommu=on iommu=pt`, 2 igb NICs on
a NAT network), vm-manager installed in L1 with `scripts/setup.sh` (libvirt 11.10, QEMU 10.1); L2 = Debian 13 in L1.

- Host checks all green in L1 (DMAR, 28 groups, iommu=pt, interrupt remapping, vfio-pci, helper 3); each igbvf VF alone
  in its IOMMU group. A fake sysfs tree exercised the failing paths (no `intel_iommu=on` on RHEL → grubby command,
  mlx5 with `sriov_totalvfs` 0 → mstconfig, admin-down PF, a VF sharing its group → plain-words refusal).
- VF count, trust on / spoofchk off (all VFs), persistence → `sriov.conf` + unit; **L1 rebooted: 3 VFs with trust on /
  spoofchk off restored by `vm-manager-sriov.service`**, the non-persistent PF came back with 0, the pool autostarted
  and L2 started with its 3 VFs.
- Pool VLAN 100 → L2's VF tagged 100 (`ip link` on the PF), L2 pinged L1's `eth2.100` across L0's bridge: QEMU's igb
  does insert the PF-set VLAN. NIC override VLAN 300 / 251 on hot-plugged VFs.
- trust on: L2 changed its VF MAC and kept traffic; trust off: "Cannot assign requested address".
- Pool exhausted → "No free VF in pool …"; VM needing 4 VFs from a 3-VF pool → "Not enough VFs …"; VF count change
  while VFs are in VMs → refused with the VF list.
- `e2e/sriov-real.js` (UI: Host checks, VF count, switches, pool + VLAN form, NIC VLAN; host checks over SSH) passes;
  `examples/opentofu/sriov-pool` apply / no-change plan / in-place trust change / refused count change / destroy.

**Not verified (needs a real SR-IOV NIC + IOMMU host)**: Intel ixgbe / i40e / ice and mlx5 VFs passed through (driver
names, firmware limits, MAC/VLAN set by libvirt on those PFs, trust semantics per driver), the boot ordering of
`vm-manager-sriov.service` vs slow PF drivers (ice DDP load, mlx5 firmware init: 90 s wait), ACS-less slots on real
boards, VLAN tagging reaching a real switch, ENOMEM / BIOS "SR-IOV Global Enable" error message when MMIO is short.

## Sources

- Red Hat, *Configuring the SR-IOV Network Operator* (`enableOperatorWebhook`, `disableDrain`; disable the webhook to use unsupported devices): https://docs.okd.io/4.20/networking/networking_operators/sr-iov-operator/configuring-sriov-operator.html , https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/networking_operators/sr-iov-operator
- Red Hat KCS 7010183, *Configuring the SR-IOV Network Operator to use an unsupported NIC*: https://access.redhat.com/articles/7010183
- Upstream supported NIC list (has `Intel_ixgbe_82576: "8086 10c9 10ca"`): https://github.com/k8snetworkplumbingwg/sriov-network-operator/blob/master/deployment/sriov-network-operator-chart/templates/configmap.yaml
- Upstream "adding unsupported NICs" (ConfigMap format `<vendor> <pf device> <vf device>`, restart config daemon + webhook): https://github.com/k8snetworkplumbingwg/sriov-network-operator/blob/master/doc/supported-hardware.md
- Discovery skips unsupported models unless dev mode (`DiscoverSriovDevices`): https://github.com/k8snetworkplumbingwg/sriov-network-operator/blob/master/pkg/host/internal/sriov/sriov.go
- OpenShift OLM ConfigMap (no 82576): https://github.com/openshift/sriov-network-operator/blob/master/manifests/stable/supported-nic-ids_v1_configmap.yaml
- sriov-network-device-plugin selectors: https://github.com/k8snetworkplumbingwg/sriov-network-device-plugin
- QEMU igb device: https://www.qemu.org/docs/master/system/devices/igb.html ; libvirt `<iommu>` / hostdev networks: https://libvirt.org/formatdomain.html#iommu-devices , https://libvirt.org/formatnetwork.html
