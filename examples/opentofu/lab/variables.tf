variable "endpoint" {
  description = "VM Manager URL"
  type        = string
  default     = "http://127.0.0.1:8000"
}

variable "nodes" {
  description = "VMs to create, keyed by name (also the hostname and DHCP name)"
  type = map(object({
    mac     = string
    ip      = string
    memory  = optional(number, 1024)
    vcpu    = optional(number, 1)
    running = optional(bool, true)
  }))
  default = {
    "tofu-web" = { mac = "52:54:00:c8:00:10", ip = "192.168.200.10" }
    "tofu-db"  = { mac = "52:54:00:c8:00:11", ip = "192.168.200.11", memory = 2048 }
  }
}

variable "username" {
  type    = string
  default = "admin"
}

variable "password" {
  description = "Console password for var.username (SSH password login is enabled when set)"
  type        = string
  default     = null
  sensitive   = true
}

variable "ssh_public_key" {
  description = "e.g. file(\"~/.ssh/id_ed25519.pub\") contents"
  type        = string
  default     = ""
}

variable "keyboard" {
  description = "Guest console keyboard layout (the web console sends physical keys)"
  type        = string
  default     = "fr"
}
