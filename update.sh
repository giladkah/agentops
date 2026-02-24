#!/bin/bash
# AgentOps Update Script v0.3.0
# Usage: bash update.sh [path-to-tar.gz]
#
# This script:
# 1. Backs up your venv
# 2. Replaces the agentops directory
# 3. Restores your venv
# 4. Deletes the DB (required for schema changes)
# 5. Starts the app

set -e

INSTALL_DIR="${HOME}/tools/agentops"
REPO_DIR="${HOME}/code/shield"
TAR_FILE="${1:-${HOME}/Downloads/agentops.tar.gz}"

echo "🤖 AgentOps Updater v0.3.0"
echo "=========================="
echo "Install dir: ${INSTALL_DIR}"
echo "Tar file:    ${TAR_FILE}"
echo ""

if [ ! -f "$TAR_FILE" ]; then
  echo "❌ Tar file not found: ${TAR_FILE}"
  echo "   Download the tar.gz and try again, or pass the path as argument:"
  echo "   bash update.sh /path/to/agentops.tar.gz"
  exit 1
fi

# Backup venv
if [ -d "${INSTALL_DIR}/venv" ]; then
  echo "📦 Backing up venv..."
  mv "${INSTALL_DIR}/venv" /tmp/agentops-venv-backup
fi

# Remove old install
if [ -d "${INSTALL_DIR}" ]; then
  echo "🗑  Removing old install..."
  rm -rf "${INSTALL_DIR}"
fi

# Extract new version
echo "📂 Extracting new version..."
cd "$(dirname ${INSTALL_DIR})"
tar -xzf "${TAR_FILE}"

# Restore venv
if [ -d /tmp/agentops-venv-backup ]; then
  echo "📦 Restoring venv..."
  mv /tmp/agentops-venv-backup "${INSTALL_DIR}/venv"
fi

# Delete DB (required for schema/seed changes)
echo "🗄  Deleting old database..."
rm -f "${INSTALL_DIR}/instance/agentops.db"

# Verify
echo ""
echo "✅ Updated! Version check:"
grep "v0\." "${INSTALL_DIR}/templates/dashboard.html" | grep -o "v[0-9.]*" | head -1
echo ""

echo "🚀 Starting AgentOps..."
cd "${INSTALL_DIR}"
source venv/bin/activate
python app.py --repo "${REPO_DIR}"
