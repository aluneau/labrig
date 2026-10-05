variable "endpoint" {
  description = "VM Manager URL"
  type        = string
  default     = "http://127.0.0.1:8000"
}

variable "name" {
  description = "Group name (VMs: <name>-rtr, <name>-<member>)"
  type        = string
  default     = "tofu-lab-group"
}

variable "cidr" {
  type    = string
  default = "10.42.60.0/24"
}

variable "members" {
  description = "Members keyed by name; ip = null lets the app assign one"
  type = map(object({
    memory = optional(number, 1024)
    ip     = optional(string)
  }))
  default = {
    web1 = { ip = "10.42.60.10" }
    db1  = {}
  }
}

variable "dns_records" {
  description = "Extra A records, name (relative to the domain) => IP"
  type        = map(string)
  default = {
    "api.ocp"    = "10.42.60.50"
    "*.apps.ocp" = "10.42.60.51"
  }
}

variable "password" {
  type      = string
  default   = null
  sensitive = true
}

variable "ssh_public_key" {
  type    = string
  default = ""
}

variable "keyboard" {
  type    = string
  default = "fr"
}
