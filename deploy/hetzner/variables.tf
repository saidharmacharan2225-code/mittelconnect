variable "hcloud_token" {
  description = "Hetzner Cloud API token (read/write) for the project that hosts MittelConnect. Pass via TF_VAR_hcloud_token."
  type        = string
  sensitive   = true
}

variable "location" {
  description = "Hetzner location. Restricted to EU data centres: nbg1 (Nuremberg), fsn1 (Falkenstein), hel1 (Helsinki)."
  type        = string
  default     = "nbg1"

  validation {
    condition     = contains(["nbg1", "fsn1", "hel1"], var.location)
    error_message = "location must be an EU location: nbg1, fsn1 or hel1."
  }
}

variable "network_zone" {
  description = "Hetzner network zone matching the location (all EU locations are in eu-central)."
  type        = string
  default     = "eu-central"
}

variable "default_server_type" {
  description = "Server type for sites that do not set their own. Check current types with `hcloud server-type list`."
  type        = string
  default     = "cx23"
}

variable "image" {
  description = "Operating system image."
  type        = string
  default     = "ubuntu-24.04"
}

variable "admin_ssh_public_key" {
  description = "OpenSSH public key of the operator; installed for the 'deploy' user (root login is disabled)."
  type        = string
}

variable "admin_cidrs" {
  description = "Source networks allowed to reach SSH (office or VPN egress addresses). Never 0.0.0.0/0 in production."
  type        = list(string)

  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrhost(c, 0))])
    error_message = "admin_cidrs must contain at least one valid CIDR."
  }
}

variable "private_network_cidr" {
  description = "Hetzner private network shared by all site hosts and the monitoring server."
  type        = string
  default     = "10.42.0.0/16"
}

variable "private_subnet_cidr" {
  description = "Subnet for MittelConnect hosts inside the private network."
  type        = string
  default     = "10.42.1.0/24"
}

variable "wireguard_port" {
  description = "UDP port of the WireGuard site-to-site tunnel."
  type        = number
  default     = 51820
}

variable "enable_backups" {
  description = "Enable Hetzner automatic server backups (7 rolling daily images, +20% of server price)."
  type        = bool
  default     = true
}

variable "sites" {
  description = <<-EOT
    One isolated host per customer site, keyed by site name (lowercase, digits, dashes).
      host_index          unique 1..200, picks the private IP (10.42.1.<10+index>)
      plant_wg_public_key WireGuard public key of the plant-side peer
      plant_lan_cidrs     plant networks the middleware must reach (DB servers, on-prem SAP)
      plant_endpoint      "host:port" of the plant peer, or null when the plant dials in
      wg_address          tunnel address of this host
      wg_peer_address     tunnel address of the plant peer (/32)
      volume_size_gb      data volume for outbox and dead letters
      server_type         overrides default_server_type
  EOT
  type = map(object({
    host_index          = number
    plant_wg_public_key = string
    plant_lan_cidrs     = list(string)
    plant_endpoint      = optional(string)
    wg_address          = optional(string, "10.99.0.1/24")
    wg_peer_address     = optional(string, "10.99.0.2/32")
    volume_size_gb      = optional(number, 20)
    server_type         = optional(string)
  }))

  validation {
    condition     = alltrue([for name, _ in var.sites : can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", name))])
    error_message = "Site names must be 3-32 characters of lowercase letters, digits and dashes."
  }

  validation {
    condition     = length(distinct([for s in values(var.sites) : s.host_index])) == length(var.sites)
    error_message = "Every site needs a unique host_index."
  }

  validation {
    condition     = alltrue([for s in values(var.sites) : s.host_index >= 1 && s.host_index <= 200])
    error_message = "host_index must be between 1 and 200."
  }

  validation {
    condition     = alltrue([for s in values(var.sites) : can(regex("^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$", s.plant_wg_public_key))])
    error_message = "plant_wg_public_key must be a base64 WireGuard public key (44 characters)."
  }

  validation {
    condition     = alltrue(flatten([for s in values(var.sites) : [for c in s.plant_lan_cidrs : can(cidrhost(c, 0))]]))
    error_message = "plant_lan_cidrs must be valid CIDRs."
  }

  validation {
    condition     = alltrue([for s in values(var.sites) : s.volume_size_gb >= 10])
    error_message = "volume_size_gb must be at least 10 (Hetzner minimum)."
  }
}
