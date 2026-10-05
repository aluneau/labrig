# SR-IOV labs with VM Manager

Two ways to give VMs SR-IOV NICs:

| | Emulated (any host, e.g. a gaming rig without IOMMU) | Real hardware (RHEL lab host with an SR-IOV NIC) |
|---|---|---|
| What the VM gets | an **igb** NIC: QEMU's Intel 82576 (PF `8086:10c9`), up to **7 VFs** created *inside* the guest (`8086:10ca`, driver `igbvf`) | a **VF of the host NIC** passed through (PCI, vfio) from an "SR-IOV VF pool" network |
| Host needs | QEMU ≥ 8.0 (igb model), libvirt that knows `igb` | IOMMU on (`intel_iommu=on iommu=pt`, VT-d in firmware), a PF with `sriov_totalvfs` > 0 |
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

## 3. Real SR-IOV NICs: VF pool networks (RHEL lab hosts)

1. **Host page → SR-IOV**: the IOMMU state and every NIC with `sriov_totalvfs` > 0 (driver, ids, VFs and their drivers).
   Set the VF count there (`PUT /api/v1/hosts/sriov/{pf} {num_vfs}`): the app runs the root helper
   `sriov-set-numvfs <pf> <n>` (whitelisted, validated: PCI netdev with sriov_totalvfs ≥ n; refused while one of
   its VFs is bound to vfio-pci, i.e. probably given to a running VM).
   The count is not persistent across host reboots: persist it with a udev rule, e.g.
   `/etc/udev/rules.d/70-sriov.rules`: `ACTION=="add", SUBSYSTEM=="net", KERNEL=="ens1f0", ATTR{device/sriov_numvfs}="8"`.
   (We use the helper rather than writing udev rules from the app: no root-owned config generated by a web app, and
   the same pkexec helper already exists; persistence stays an explicit admin choice.)
2. **Networks → Create network → "SR-IOV VF pool"**, pick the PF: libvirt network
   `<forward mode='hostdev' managed='yes'><pf dev='…'/></forward>` (no bridge / IP / DHCP: the VF is on the physical
   network).
3. **Add a NIC on that network** (VM details or `extra_nics`): libvirt hands out a free VF, binds it to vfio-pci when
   the VM starts and gives it back afterwards, and sets the MAC on the VF. Hot-plug and hot-unplug work.
   Without a host IOMMU the app refuses with the reason (and the Host page says how to enable it).

Verified nested (L0 = this rig, no IOMMU; L1 = AlmaLinux 10 VM created by the app with an igb NIC + vIOMMU + kernel
args; vm-manager installed in L1 from `scripts/package.sh` + `setup.sh`): in L1's app, set 3 VFs on `eth1`
(helper via pkexec), created the pool `e2e-h-vfpool`, created a Debian 13 L2 with a VF NIC → L2 sees
`82576 Virtual Function [8086:10ca]` bound to `igbvf`, gets a DHCP lease from L0's network through the VF, pings L0 and
the internet. Hot-plugging a second VF into the running L2 and hot-unplugging it also worked (VF back to igbvf in L1).
This rig itself has no IOMMU (and a non-SR-IOV Realtek NIC): the pool path is only testable nested here.

## Sources

- Red Hat, *Configuring the SR-IOV Network Operator* (`enableOperatorWebhook`, `disableDrain`; disable the webhook to use unsupported devices): https://docs.okd.io/4.20/networking/networking_operators/sr-iov-operator/configuring-sriov-operator.html , https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/networking_operators/sr-iov-operator
- Red Hat KCS 7010183, *Configuring the SR-IOV Network Operator to use an unsupported NIC*: https://access.redhat.com/articles/7010183
- Upstream supported NIC list (has `Intel_ixgbe_82576: "8086 10c9 10ca"`): https://github.com/k8snetworkplumbingwg/sriov-network-operator/blob/master/deployment/sriov-network-operator-chart/templates/configmap.yaml
- Upstream "adding unsupported NICs" (ConfigMap format `<vendor> <pf device> <vf device>`, restart config daemon + webhook): https://github.com/k8snetworkplumbingwg/sriov-network-operator/blob/master/doc/supported-hardware.md
- Discovery skips unsupported models unless dev mode (`DiscoverSriovDevices`): https://github.com/k8snetworkplumbingwg/sriov-network-operator/blob/master/pkg/host/internal/sriov/sriov.go
- OpenShift OLM ConfigMap (no 82576): https://github.com/openshift/sriov-network-operator/blob/master/manifests/stable/supported-nic-ids_v1_configmap.yaml
- sriov-network-device-plugin selectors: https://github.com/k8snetworkplumbingwg/sriov-network-device-plugin
- QEMU igb device: https://www.qemu.org/docs/master/system/devices/igb.html ; libvirt `<iommu>` / hostdev networks: https://libvirt.org/formatdomain.html#iommu-devices , https://libvirt.org/formatnetwork.html
