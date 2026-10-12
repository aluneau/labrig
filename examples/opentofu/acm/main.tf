# Red Hat Advanced Cluster Management: a single-node OpenShift hub (ACM add-on) and a kubeadm cluster in its
# own lab group, imported as a managed cluster (docs/openshift.md "ACM hub").
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply                  # ~90 min (OpenShift install, ACM, kubeadm, import)
#   tofu output -raw hub_kubeconfig > hub.yaml && oc --kubeconfig hub.yaml get managedclusters
#
# Needs ~42 GiB of free RAM: hub 32 GiB (OpenShift + ACM), kubeadm 2 x 4 GiB, 2 routers.

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
  default = "tofu-acm"
}

variable "pull_secret_path" {
  type    = string
  default = "~/pull-secret.json"
}

provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_openshift_pull_secret" "this" {
  path = var.pull_secret_path
}

resource "vmmanager_cloud_image" "debian" {
  distribution    = "debian"
  version         = "13"
  keep_on_destroy = true
}

resource "vmmanager_cloud_image" "alma" {
  distribution    = "almalinux"
  version         = "9"
  keep_on_destroy = true
}

resource "vmmanager_cluster" "hub" {
  name = "${var.name}-hub"
  type = "openshift"
  openshift = {
    topology = "sno"
    storage  = "lvms"
    acm      = true # ACM operator + MultiClusterHub; enabling it later on an installed cluster works in place
  }
  depends_on = [vmmanager_openshift_pull_secret.this, vmmanager_cloud_image.alma]
}

# The managed cluster lives in its own lab group (one OpenShift cluster per group, and none with kubeadm)
resource "vmmanager_cluster" "kadm" {
  name           = "${var.name}-kadm"
  type           = "kubeadm"
  ctlplanes      = 1
  workers        = 1
  ctlplane_memory = 4096
  worker_memory   = 4096
  cloud_image_id = vmmanager_cloud_image.debian.id
  depends_on     = [vmmanager_cloud_image.alma]
}

# Waits until the ManagedCluster is Joined and Available; destroy detaches it (the hub removes the klusterlet)
resource "vmmanager_acm_managed_cluster" "kadm" {
  hub_id     = vmmanager_cluster.hub.id
  cluster_id = vmmanager_cluster.kadm.id
}

output "hub_kubeconfig" {
  value     = vmmanager_cluster.hub.kubeconfig
  sensitive = true
}

output "console_url" {
  value = vmmanager_cluster.hub.console_url
}

output "import_method" {
  value = vmmanager_acm_managed_cluster.kadm.import_method
}
