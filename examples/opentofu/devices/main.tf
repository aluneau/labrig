# A VM with an extra data disk, an ISO in its CD-ROM drive and an explicit boot order.
#
#   make -C ../../../opentofu_provider install   # once
#   tofu init && tofu apply -var iso=/var/lib/libvirt/images/<some>.iso   (see GET /api/v1/storage/isos)
#
# Then try in place: data_disk_gb = 20 (grown live), cdrom = "" (eject), boot_order = ["cdrom", "hd"].

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
  description = "Prefix of the VM name"
  type        = string
  default     = "devices"
}

variable "iso" {
  description = "ISO volume path to insert (\"\" = empty drive)"
  type        = string
  default     = ""
}

variable "boot_order" {
  type    = list(string)
  default = ["hd", "cdrom"]
}

variable "data_disk_gb" {
  description = "Size of the extra disk (can only grow)"
  type        = number
  default     = 10
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
  cdrom          = var.iso
  boot_order     = var.boot_order

  cloud_init = {
    username = "admin"
    ssh_keys = fileexists(local.ssh_key) ? [file(local.ssh_key)] : []
  }
}

resource "vmmanager_disk" "data" {
  vm_id   = vmmanager_vm.vm.id
  size_gb = var.data_disk_gb
}

locals {
  ssh_key = pathexpand("~/.ssh/id_ed25519.pub")
}

output "data_disk" {
  value = "${vmmanager_disk.data.target} (${vmmanager_disk.data.path})"
}
