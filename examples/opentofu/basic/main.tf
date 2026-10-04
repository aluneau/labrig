# The smallest useful config: one Debian VM called "my-vm" on the default network.
#
#   make -C ../../../opentofu_provider install   # once
#   tofu init && tofu apply      # asks for the admin password
#   ssh admin@<ip printed at the end>   (or log in from the web console)

terraform {
  required_providers {
    vmmanager = {
      source  = "local/vmmanager"
      version = "0.1.0"
    }
  }
}

provider "vmmanager" {} # http://127.0.0.1:8000 (or $VMMANAGER_ENDPOINT)

resource "vmmanager_cloud_image" "debian" {
  distribution    = "debian"
  version         = "13"
  keep_on_destroy = true
}

resource "vmmanager_vm" "my_vm" {
  name           = "my-vm"
  cloud_image_id = vmmanager_cloud_image.debian.id
  wait_for_ip    = true

  cloud_init = {
    username = "admin"
    password = var.password
    ssh_keys = fileexists(local.ssh_key) ? [file(local.ssh_key)] : []
  }
}

variable "password" {
  description = "Password for the admin user"
  type        = string
  sensitive   = true
}

locals {
  ssh_key = pathexpand("~/.ssh/id_ed25519.pub")
}

output "ip" {
  value = vmmanager_vm.my_vm.ip_addresses[0]
}
