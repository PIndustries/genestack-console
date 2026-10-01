variable "access_key" {
  type      = string
  sensitive = true
}

variable "secret_key" {
  type      = string
  sensitive = true
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "node_count" {
  type    = number
  default = 1
}

variable "flavor" {
  type    = string
  default = "c5.metal"
}

variable "name_prefix" {
  type    = string
  default = "gs"
}

variable "role" {
  type    = string
  default = "compute"
}

variable "ami_id" {
  type    = string
  default = ""
}
