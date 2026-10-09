# A VM for SR-IOV labs: a virtio NIC on the default network + an igb NIC (Intel 82576, emulated SR-IOV,
# up to 7 VFs in the guest) on its own network, a virtual IOMMU and intel_iommu=on iommu=pt in the guest.
#
#   make -C ../../../opentofu_provider install   # once
#   tofu init && tofu apply
#
# In the guest:  echo 4 | sudo tee /sys/class/net/enp2s0/device/sriov_numvfs   -> 4 igbvf VFs (8086:10ca)
# Then try in place: link_state = "down" on the igb NIC, iommu = false (applies after power off + start).
# See docs/sriov.md for OpenShift / Kubernetes SR-IOV operator settings.

terraform {
  required_providers {
    vmmanager = {
      source  = "local/vmmanager"
      version = "0.1.0"
    }
  }
}

# Authentication: export VMMANAGER_TOKEN=<API token> (web UI: user menu > API tokens, or
# backend/venv/bin/python -m app.cli token create --user $USER --name opentofu), or set token = "...".
provider "vmmanager" {} # http://127.0.0.1:8000 (or $VMMANAGER_ENDPOINT)

variable "prefix" {
  description = "Prefix of the VM and network names"
  type        = string
  default     = "sriov"
}

variable "subnet" {
  description = "First three octets of the SR-IOV network's /24"
  type        = string
  default     = "192.168.210"
}

variable "igb_link_state" {
  type    = string
  default = "up"
}

variable "iommu" {
  type    = bool
  default = true
}

resource "vmmanager_cloud_image" "debian" {
  distribution    = "debian"
  version         = "13"
  keep_on_destroy = true
}

# The network the igb PF (and its VFs) are plugged into
resource "vmmanager_network" "sriov" {
  name       = "${var.prefix}-net"
  mode       = "nat"
  ip_address = "${var.subnet}.1"
  prefix     = 24
}

resource "vmmanager_vm" "vm" {
  name              = "${var.prefix}-vm"
  memory            = 2048
  vcpu              = 2
  disk_size         = 10
  cloud_image_id    = vmmanager_cloud_image.debian.id
  iommu             = var.iommu
  guest_kernel_args = "intel_iommu=on iommu=pt"

  cloud_init = {
    username = "admin"
    ssh_keys = fileexists(local.ssh_key) ? [file(local.ssh_key)] : []
  }
}

resource "vmmanager_nic" "igb" {
  vm_id      = vmmanager_vm.vm.id
  network    = vmmanager_network.sriov.name
  model      = "igb"
  link_state = var.igb_link_state
}

locals {
  ssh_key = pathexpand("~/.ssh/id_ed25519.pub")
}

output "igb_mac" {
  value = vmmanager_nic.igb.mac
}
