# A lab group: isolated network + EL router (DHCP, DNS, NAT) + Debian members with fixed
# addresses and DNS names. Needs ready cloud images (an EL one for the router, Debian 13 for
# the members): download them on the Storage page or with vmmanager_cloud_image.
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply
#
# Adding/removing a member, a dns_record or a dhcp_host is applied in place (live on the router).

terraform {
  required_providers {
    vmmanager = {
      source = "local/vmmanager"
    }
  }
}

provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_group" "lab" {
  name         = var.name
  cidr         = var.cidr
  domain       = "${var.name}.lab"
  router_image = "almalinux-9"

  cloud_init = {
    username = "admin"
    password = var.password
    ssh_keys = var.ssh_public_key == "" ? [] : [var.ssh_public_key]
    keyboard = var.keyboard
  }

  dynamic "member" {
    for_each = var.members
    content {
      name   = member.key
      image  = "debian-13"
      memory = member.value.memory
      ip     = member.value.ip
    }
  }

  dynamic "dhcp_host" {
    for_each = var.dhcp_hosts
    content {
      mac      = dhcp_host.key
      ip       = dhcp_host.value.ip
      hostname = dhcp_host.value.hostname
    }
  }

  dynamic "dns_record" {
    for_each = var.dns_records
    content {
      name = dns_record.key
      a    = dns_record.value
    }
  }
}
