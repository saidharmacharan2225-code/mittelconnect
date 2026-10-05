# =============================================================================
# MittelConnect on Hetzner Cloud (EU): one hardened host per customer site.
#
#   - private network for monitoring (Prometheus scrapes mcmon on :9464)
#   - cloud firewall: SSH only from admin_cidrs, WireGuard UDP, nothing else
#   - per site: server + separate data volume + WireGuard tunnel to the plant
#   - delete/rebuild protection and daily backups on every site host
#
# The middleware is installed afterwards with scripts/deploy.sh; Terraform
# never sees database or SAP credentials.
# =============================================================================

locals {
  common_labels = {
    "app"        = "mittelconnect"
    "managed-by" = "terraform"
  }
  site_private_ip = { for name, s in var.sites : name => cidrhost(var.private_subnet_cidr, 10 + s.host_index) }
}

resource "hcloud_ssh_key" "admin" {
  name       = "mittelconnect-admin"
  public_key = var.admin_ssh_public_key
  labels     = local.common_labels
}

resource "hcloud_network" "main" {
  name     = "mittelconnect"
  ip_range = var.private_network_cidr
  labels   = local.common_labels
}

resource "hcloud_network_subnet" "hosts" {
  network_id   = hcloud_network.main.id
  type         = "cloud"
  network_zone = var.network_zone
  ip_range     = var.private_subnet_cidr
}

resource "hcloud_firewall" "site" {
  name   = "mittelconnect-site"
  labels = local.common_labels

  rule {
    description = "SSH from operator networks"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_cidrs
  }

  rule {
    description = "WireGuard tunnel to customer plants (silently drops unauthenticated packets)"
    direction   = "in"
    protocol    = "udp"
    port        = tostring(var.wireguard_port)
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "ICMP for path MTU discovery and diagnostics"
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }
}

resource "hcloud_placement_group" "sites" {
  name   = "mittelconnect-sites"
  type   = "spread"
  labels = local.common_labels
}

resource "hcloud_volume" "data" {
  for_each = var.sites

  name              = "mc-${each.key}-data"
  size              = each.value.volume_size_gb
  location          = var.location
  format            = "ext4"
  delete_protection = true
  labels            = merge(local.common_labels, { "site" = each.key })
}

resource "hcloud_server" "site" {
  for_each = var.sites

  name               = "mc-${each.key}"
  server_type        = coalesce(each.value.server_type, var.default_server_type)
  image              = var.image
  location           = var.location
  ssh_keys           = [hcloud_ssh_key.admin.id]
  firewall_ids       = [hcloud_firewall.site.id]
  placement_group_id = hcloud_placement_group.sites.id
  backups            = var.enable_backups
  delete_protection  = true
  rebuild_protection = true
  labels             = merge(local.common_labels, { "site" = each.key })

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  network {
    network_id = hcloud_network.main.id
    ip         = local.site_private_ip[each.key]
  }

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    site_name            = each.key
    private_ip           = local.site_private_ip[each.key]
    admin_ssh_public_key = trimspace(var.admin_ssh_public_key)
    volume_device        = hcloud_volume.data[each.key].linux_device
    wg_port              = var.wireguard_port
    wg_address           = each.value.wg_address
    wg_peer_address      = each.value.wg_peer_address
    plant_wg_public_key  = each.value.plant_wg_public_key
    plant_lan_cidrs      = join(", ", each.value.plant_lan_cidrs)
    plant_endpoint       = each.value.plant_endpoint == null ? "" : each.value.plant_endpoint
  })

  # user_data only runs on first boot; changing it must not rebuild a
  # running site host (and its outbox) behind the operator's back.
  lifecycle {
    ignore_changes = [user_data, image, ssh_keys]
  }

  depends_on = [hcloud_network_subnet.hosts]
}

resource "hcloud_volume_attachment" "data" {
  for_each = var.sites

  volume_id = hcloud_volume.data[each.key].id
  server_id = hcloud_server.site[each.key].id
  automount = false
}
