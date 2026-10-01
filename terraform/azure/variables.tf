variable "tenant_id" {
  type      = string
  sensitive = true
}

variable "client_id" {
  type      = string
  sensitive = true
}

variable "client_secret" {
  type      = string
  sensitive = true
}

variable "subscription_id" {
  type      = string
  sensitive = true
}

variable "region" {
  type    = string
  default = "eastus"
}

variable "node_count" {
  type    = number
  default = 1
}

variable "flavor" {
  type    = string
  default = "Standard_D32s_v5"
}

variable "name_prefix" {
  type    = string
  default = "gs"
}

variable "role" {
  type    = string
  default = "compute"
}

variable "admin_username" {
  type    = string
  default = "ubuntu"
}

variable "admin_ssh_public_key" {
  type    = string
  default = ""
}
