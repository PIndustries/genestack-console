terraform {
  required_version = ">= 1.3.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

locals {
  zone = var.zone != "" ? var.zone : "${var.region}-a"
}

provider "google" {
  project     = var.project_id
  region      = var.region
  zone        = local.zone
  credentials = var.service_account_json != "" ? var.service_account_json : null
}

resource "google_compute_instance" "node" {
  count        = var.node_count
  name         = "${var.name_prefix}-${count.index + 1}"
  machine_type = var.flavor
  zone         = local.zone
  boot_disk {
    initialize_params {
      image = var.image
    }
  }
  network_interface {
    network = "default"
    access_config {}
  }
  labels = {
    role = var.role
  }
}
