output "hosts" {
  value = [
    for i, n in azurerm_linux_virtual_machine.node : {
      hostname = n.name
      ip       = azurerm_network_interface.node[i].private_ip_address
      role     = var.role
    }
  ]
}
