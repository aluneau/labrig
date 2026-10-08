# An air-gapped kubeadm cluster (1 control plane + 1 worker): the group router runs a mirror registry
# holding the cluster's images, the nodes pull through containerd mirrors and the group's egress is
# blocked (docs/disconnected.md, "Kubernetes (kubeadm)").
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply                  # first time ~25-40 min (mirror-registry download on the router)
#   tofu output -raw kubeconfig > air.yaml
#   kubectl --kubeconfig air.yaml run web --image=nginx:alpine          # mirrored: Running
#   kubectl --kubeconfig air.yaml run bb --image=busybox:1.37 -- sleep 1d # not mirrored: ImagePullBackOff
#
# More images later: the cluster page ("Mirror more images") or
#   curl -X POST http://127.0.0.1:8000/api/v1/clusters/<id>/mirror -H 'content-type: application/json' \
#        -d '{"images": ["busybox:1.37"]}'

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
  default = "tofu-air"
}

provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_cloud_image" "debian" {
  distribution    = "debian"
  version         = "13"
  keep_on_destroy = true
}

resource "vmmanager_cloud_image" "alma" {
  distribution    = "almalinux"
  version         = "9"
  keep_on_destroy = true # the group router
}

resource "vmmanager_cluster" "air" {
  name           = var.name
  type           = "kubeadm"
  ctlplanes      = 1
  workers        = 1
  cloud_image_id = vmmanager_cloud_image.debian.id
  disconnected   = true
  mirror_images  = ["docker.io/library/redis:7-alpine"]
  depends_on     = [vmmanager_cloud_image.alma]
}

output "api_endpoint" {
  value = vmmanager_cluster.air.api_endpoint
}

output "kubeconfig" {
  value     = vmmanager_cluster.air.kubeconfig
  sensitive = true
}
