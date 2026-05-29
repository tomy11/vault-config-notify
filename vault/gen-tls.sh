#!/bin/sh
# สร้าง self-signed TLS certificate สำหรับ Vault
# รันครั้งเดียวก่อน docker compose up

set -e

TLS_DIR="$(dirname "$0")/tls"
mkdir -p "$TLS_DIR"

echo "Generating self-signed TLS certificate for Vault..."

openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout "$TLS_DIR/vault.key" \
  -out "$TLS_DIR/vault.crt" \
  -days 365 \
  -subj "/CN=vault" \
  -addext "subjectAltName=DNS:vault,DNS:localhost,IP:127.0.0.1"

chmod 600 "$TLS_DIR/vault.key"
chmod 644 "$TLS_DIR/vault.crt"

echo "Done! TLS files created:"
echo "  $TLS_DIR/vault.key"
echo "  $TLS_DIR/vault.crt"
