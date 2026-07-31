#!/bin/bash
set -euo pipefail

# MCP Gateway Deployment Script
# Usage: sudo bash deploy.sh [REPO_URL]

REPO_URL="${1:-https://github.com/sunweini/mcp-store.git}"
INSTALL_DIR="/opt/mcp-gateway"
CONFIG_DIR="/etc/mcp-gateway"
SERVICE_USER="mcp"

echo "=== MCP Gateway Deployment ==="
echo "Target: $INSTALL_DIR"
echo "Repo: $REPO_URL"
echo ""

# 1. Check prerequisites
echo "[1/8] Checking prerequisites..."
for cmd in git python3 curl; do
  command -v $cmd >/dev/null 2>&1 || { echo "ERROR: $cmd not found"; exit 1; }
done

# Install uv if not present
if ! command -v uv >/dev/null 2>&1; then
  echo "  Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "  uv: $(uv --version)"

# Check Redis
if command -v redis-server >/dev/null 2>&1; then
  echo "  Redis: $(redis-server --version)"
elif command -v redis-cli >/dev/null 2>&1; then
  echo "  Redis client found (server should be running)"
else
  echo "  WARNING: Redis not found. Installing..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq redis-server
  elif command -v yum >/dev/null 2>&1; then
    yum install -y redis
  else
    echo "ERROR: Cannot install Redis automatically. Install manually."
    exit 1
  fi
  systemctl enable --now redis-server 2>/dev/null || systemctl enable --now redis 2>/dev/null || true
fi

# 2. Create service user
echo "[2/8] Creating service user..."
if ! id -u $SERVICE_USER >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -d $INSTALL_DIR $SERVICE_USER
  echo "  Created user: $SERVICE_USER"
else
  echo "  User $SERVICE_USER exists"
fi

# 3. Clone/update repo
echo "[3/8] Cloning repository..."
if [ -d "$INSTALL_DIR/.git" ]; then
  cd $INSTALL_DIR
  git fetch --all
  git reset --hard origin/main
  echo "  Updated existing repo"
else
  git clone $REPO_URL $INSTALL_DIR
  echo "  Cloned to $INSTALL_DIR"
fi
cd $INSTALL_DIR

# 4. Install Python dependencies
echo "[4/8] Installing Python dependencies..."
cd $INSTALL_DIR/gateway-proxy
uv sync
cd $INSTALL_DIR/gateway-admin
uv sync

# Build frontend
echo "  Building frontend..."
cd $INSTALL_DIR/gateway-admin/admin-ui
npm install --production=false
npm run build
echo "  Frontend built to dist/"

# 5. Set permissions
echo "[5/8] Setting permissions..."
chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR
chmod -R 750 $INSTALL_DIR

# 6. Install systemd services
echo "[6/8] Installing systemd services..."
cp $INSTALL_DIR/deploy/systemd/gateway-proxy.service /etc/systemd/system/
cp $INSTALL_DIR/deploy/systemd/gateway-admin.service /etc/systemd/system/
systemctl daemon-reload

# 7. Create config directory + env files
echo "[7/8] Creating config..."
mkdir -p $CONFIG_DIR

# Generate JWT secret if not exists
if [ ! -f "$CONFIG_DIR/admin.env" ]; then
  JWT_SECRET=$(openssl rand -base64 32)
  cat > $CONFIG_DIR/admin.env << EOF
JWT_SECRET=$JWT_SECRET
JWT_EXPIRES=86400
EOF
  chmod 640 $CONFIG_DIR/admin.env
  chown $SERVICE_USER:$SERVICE_USER $CONFIG_DIR/admin.env
  echo "  Generated JWT_SECRET in $CONFIG_DIR/admin.env"
else
  echo "  Config exists, skipping"
fi

# Create proxy env if not exists
if [ ! -f "$CONFIG_DIR/proxy.env" ]; then
  cat > $CONFIG_DIR/proxy.env << EOF
# Optional: set OTel collector endpoint
# OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
EOF
  chmod 640 $CONFIG_DIR/proxy.env
  chown $SERVICE_USER:$SERVICE_USER $CONFIG_DIR/proxy.env
fi

# 8. Start services
echo "[8/8] Starting services..."
systemctl enable gateway-proxy gateway-admin
systemctl restart gateway-proxy
sleep 2
systemctl restart gateway-admin
sleep 2

# Verify
echo ""
echo "=== Verification ==="
systemctl is-active gateway-proxy && echo "  gateway-proxy: OK" || echo "  gateway-proxy: FAILED"
systemctl is-active gateway-admin && echo "  gateway-admin: OK" || echo "  gateway-admin: FAILED"

echo ""
echo "=== Access ==="
echo "  Admin UI:  http://$(hostname -I | awk '{print $1}'):8081"
echo "  Proxy:     http://$(hostname -I | awk '{print $1}'):8080/mcp"
echo "  Metrics:   http://$(hostname -I | awk '{print $1}'):9465/metrics"
echo ""
echo "  Default admin: admin / admin123 (CHANGE IMMEDIATELY!)"
echo ""
echo "=== Logs ==="
echo "  journalctl -u gateway-proxy -f"
echo "  journalctl -u gateway-admin -f"
echo ""
echo "=== Deploy Complete ==="
