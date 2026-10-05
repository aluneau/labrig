# A kubeadm cluster (1 control plane + 1 worker) in a lab group whose router serves the DNS
# records and load-balances the API (haproxy).
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply                  # ~5-8 min (router first boot, then kubeadm)
#   tofu output -raw kubeconfig > kubeadm.yaml && kubectl --kubeconfig kubeadm.yaml get nodes
#
# By default the group is created for the cluster and deleted with it. Set use_existing_group = true
# to put the nodes into the vmmanager_group below instead (it keeps its other members when the
# cluster is destroyed). Change `workers` and apply again to scale in place.

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
  default = "tofu-kubeadm"
}

variable "workers" {
  type    = number
  default = 1
}

variable "ctlplanes" {
  type    = number
  default = 1 # 3: stacked etcd, haproxy on the router in front of the three API servers
}

variable "use_existing_group" {
  type    = bool
  default = false
}

provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_cloud_image" "debian" {
  distribution    = "debian"
  version         = "13"
  keep_on_destroy = true # shared download cache
}

resource "vmmanager_cloud_image" "alma" {
  distribution    = "almalinux"
  version         = "9"
  keep_on_destroy = true
}

# Only when use_existing_group = true: a lab group with a normal member, the cluster joins it
resource "vmmanager_group" "lab" {
  count        = var.use_existing_group ? 1 : 0
  name         = "${var.name}-lab"
  cidr         = "10.42.60.0/24"
  router_image = "almalinux-9"
  member {
    name  = "client"
    image = "debian-13"
  }
  depends_on = [vmmanager_cloud_image.debian, vmmanager_cloud_image.alma]
}

resource "vmmanager_cluster" "k8s" {
  name           = var.name
  type           = "kubeadm"
  ctlplanes      = var.ctlplanes
  workers        = var.workers
  cloud_image_id = vmmanager_cloud_image.debian.id
  group_id       = var.use_existing_group ? vmmanager_group.lab[0].id : null
  # version = "v1.36"   # default: the server's pinned Kubernetes minor
  depends_on = [vmmanager_cloud_image.alma] # the group router is AlmaLinux
}

output "api_endpoint" {
  value = vmmanager_cluster.k8s.api_endpoint
}

output "group_id" {
  value = vmmanager_cluster.k8s.group_id
}

output "node_ips" {
  value = vmmanager_cluster.k8s.node_ips
}

output "kubeconfig" {
  value     = vmmanager_cluster.k8s.kubeconfig
  sensitive = true
}
