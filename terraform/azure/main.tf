terraform {
  required_version = ">= 1.3.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
  tenant_id       = var.tenant_id
  client_id       = var.client_id
  client_secret   = var.client_secret
  subscription_id = var.subscription_id
}

resource "azurerm_resource_group" "gs" {
  name     = "${var.name_prefix}-rg"
  location = var.region
}

resource "azurerm_virtual_network" "gs" {
  name                = "${var.name_prefix}-vnet"
  address_space       = ["10.250.0.0/16"]
  location            = var.region
  resource_group_name = azurerm_resource_group.gs.name
}

resource "azurerm_subnet" "gs" {
  name                 = "${var.name_prefix}-subnet"
  resource_group_name  = azurerm_resource_group.gs.name
  virtual_network_name = azurerm_virtual_network.gs.name
  address_prefixes     = ["10.250.1.0/24"]
}

resource "azurerm_network_interface" "node" {
  count               = var.node_count
  name                = "${var.name_prefix}-nic-${count.index + 1}"
  location            = var.region
  resource_group_name = azurerm_resource_group.gs.name
  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.gs.id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_linux_virtual_machine" "node" {
  count               = var.node_count
  name                = "${var.name_prefix}-${count.index + 1}"
  location            = var.region
  resource_group_name = azurerm_resource_group.gs.name
  size                = var.flavor
  admin_username      = var.admin_username
  network_interface_ids = [
    azurerm_network_interface.node[count.index].id,
  ]
  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }
  admin_ssh_key {
    username   = var.admin_username
    public_key = var.admin_ssh_public_key
  }
}
