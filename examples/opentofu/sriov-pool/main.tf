# Real SR-IOV (a RHEL lab host with an SR-IOV NIC, IOMMU on): VFs of a host physical function handed to a VM.
#   - vmmanager_sriov_pf: VF count on the PF, VF trust / spoof checking, kept across host reboots
#   - vmmanager_network (mode hostdev): the VF pool, with a VLAN tag set on every VF
#   - a VM with a VF NIC from the pool (VLAN override on the NIC)
#
#   make -C ../../../opentofu_provider install   # once
#   tofu init && tofu apply -var pf=ens1f0       # see GET /api/v1/hosts/sriov (Host page, SR-IOV) for PF names
#
# Then try: num_vfs = 8 (refused while a VF is in a running VM), trust = false, vm_vlan = null.
# See docs/sriov.md "Real hardware (RHEL lab host)".

terraform {
  required_providers {
    vmmanager = {
      source  = "local/vmmanager"
      version = "0.1.0"
    }
  }
}

provider "vmmanager" {} # http://127.0.0.1:8000 (or $VMMANAGER_ENDPOINT)

variable "prefix" {
  type    = string
  default = "vfpool"
}

variable "pf" {
  description = "SR-IOV physical function (host interface), e.g. ens1f0"
  type        = string
}

variable "num_vfs" {
  type    = number
  default = 4
}

variable "trust" {
  description = "VF trust (guest may change its MAC: bonding, OpenShift)"
  type        = bool
  default     = true
}

variable "pool_vlan" {
  description = "VLAN tag of the pool (null = untagged)"
  type        = number
  default     = null
}

variable "vm_vlan" {
  description = "VLAN tag of the VM's VF (overrides the pool's; null = the pool's)"
  type        = number
  default     = null
}

resource "vmmanager_sriov_pf" "pf" {
  name       = var.pf
  num_vfs    = var.num_vfs
  trust      = var.trust
  spoofchk   = !var.trust
  persistent = true
}

resource "vmmanager_network" "pool" {
  name        = "${var.prefix}-${var.pf}"
  mode        = "hostdev"
  forward_dev = vmmanager_sriov_pf.pf.name
  vlan        = var.pool_vlan
}

resource "vmmanager_cloud_image" "debian" {
  distribution    = "debian"
  version         = "13"
  keep_on_destroy = true
}

resource "vmmanager_vm" "vm" {
  name           = "${var.prefix}-vm"
  memory         = 1024
  vcpu           = 1
  disk_size      = 8
  cloud_image_id = vmmanager_cloud_image.debian.id

  cloud_init = {
    username = "admin"
    ssh_keys = fileexists(local.ssh_key) ? [file(local.ssh_key)] : []
  }
}

resource "vmmanager_nic" "vf" {
  vm_id   = vmmanager_vm.vm.id
  network = vmmanager_network.pool.name
  vlan    = var.vm_vlan
}

locals {
  ssh_key = pathexpand("~/.ssh/id_ed25519.pub")
}

output "vf_mac" {
  value = vmmanager_nic.vf.mac
}
