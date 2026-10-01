variable "project_id" {
  type = string
}

variable "service_account_json" {
  type      = string
  sensitive = true
  default   = ""
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = ""
}

variable "node_count" {
  type    = number
  default = 1
}

variable "flavor" {
  type    = string
  default = "n2-highcpu-8"
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
  default = "ubuntu-os-cloud/ubuntu-2204-lts"
}
