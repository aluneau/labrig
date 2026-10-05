# A k3s cluster (1 control plane + 2 workers) on its own NAT network.
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply                  # ~2-3 min
#   tofu output -raw kubeconfig > k3s.yaml && kubectl --kubeconfig k3s.yaml get nodes
#
# Change `workers` and apply again to scale in place.

terraform {
  required_providers {
    vmmanager = {
      source  = "local/vmmanager"
      version = "0.1.0"
    }
  }
}

variable "endpoint" {
  type    = string
  default = "http://127.0.0.1:8000"
}

variable "name" {
  type    = string
  default = "tofu-k3s"
}

variable "workers" {
  type    = number
  default = 2
}

provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_cloud_image" "debian" {
  distribution    = "debian"
  version         = "13"
  keep_on_destroy = true # shared download cache
}

resource "vmmanager_cluster" "k3s" {
  name           = var.name
  type           = "k3s"
  workers        = var.workers
  cloud_image_id = vmmanager_cloud_image.debian.id
  # version = "v1.33.5+k3s1"   # default: k3s stable channel
  worker_memory = 2048
}

output "api_endpoint" {
  value = vmmanager_cluster.k3s.api_endpoint
}

output "node_ips" {
  value = vmmanager_cluster.k3s.node_ips
}

output "kubeconfig" {
  value     = vmmanager_cluster.k3s.kubeconfig
  sensitive = true
}
