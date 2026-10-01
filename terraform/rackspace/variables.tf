variable "username" {
  type = string
}

variable "api_key" {
  type      = string
  sensitive = true
}

variable "region" {
  type    = string
  default = ""
}

variable "auth_url" {
  type    = string
  default = "https://identity.api.rackspacecloud.com/v2.0/"
}

variable "tenant_name" {
  type    = string
  default = ""
}

variable "node_count" {
  type    = number
  default = 1
}

variable "flavor" {
  type    = string
  default = "general1-8"
}

variable "name_prefix" {
  type    = string
  default = "gs"
}

variable "role" {
  type    = string
  default = "compute"
}

variable "image" {
  type    = string
  default = "Ubuntu 22.04 LTS (Jammy Jellyfish) (PVHVM)"
}
