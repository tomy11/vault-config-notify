ui            = true
disable_mlock = true

# ─── Storage (ใช้ file สำหรับ single node) ───────────────────────
storage "file" {
  path = "/vault/data"
}

# ─── Listener (HTTPS) ────────────────────────────────────────────
listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_cert_file = "/vault/tls/vault.crt"
  tls_key_file  = "/vault/tls/vault.key"
}

# ─── Telemetry ───────────────────────────────────────────────────
telemetry {
  disable_hostname = true
}

# ─── General ─────────────────────────────────────────────────────
api_addr     = "https://vault:8200"
cluster_addr = "https://vault:8201"
log_level    = "info"
log_file     = "/vault/logs/vault.log"
