#!/usr/bin/env bash
# Ensemble — one-line installer
# Usage: curl -sSL https://raw.githubusercontent.com/giladkah/agentops/main/install.sh | bash
#
# What this does:
#   1. Checks for Python 3.10+ and git
#   2. Downloads the latest release tar from GitHub
#   3. Installs into ~/tools/agentops
#   4. Creates a venv and installs dependencies
#   5. Adds an `ensemble` CLI command to your shell
#   6. Launches the menubar app

set -e

# ── Config ────────────────────────────────────────────────────────────────────
REPO="giladkah/agentops"
INSTALL_DIR="$HOME/tools/agentops"
RELEASE_URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
MIN_PYTHON_MINOR=10

# ── Colors ────────────────────────────────────────────────────────────────────
BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

info()    { echo -e "${GREEN}▶${RESET} $1"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $1"; }
error()   { echo -e "${RED}✗${RESET}  $1"; exit 1; }
success() { echo -e "${GREEN}✓${RESET} $1"; }

echo ""
echo -e "${BOLD}  ⚡ Ensemble — AI Agents That Peer-Review Each Other${RESET}"
echo    "  ─────────────────────────────────────────────────"
echo ""

# ── Check OS ─────────────────────────────────────────────────────────────────
if [[ "$OSTYPE" != "darwin"* ]]; then
  error "The menubar app is macOS only. For Linux/Windows, run 'python3 app.py --repo /path/to/repo' directly."
fi

# ── Check Python ─────────────────────────────────────────────────────────────
info "Checking Python..."
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cmd" &>/dev/null; then
    VERSION=$("$cmd" -c "import sys; print(sys.version_info.minor)")
    MAJOR=$("$cmd" -c "import sys; print(sys.version_info.major)")
    if [[ "$MAJOR" -eq 3 && "$VERSION" -ge "$MIN_PYTHON_MINOR" ]]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  error "Python 3.10+ is required. Install it from https://python.org or via Homebrew: brew install python3"
fi
success "Found $($PYTHON --version)"

# ── Check git ─────────────────────────────────────────────────────────────────
info "Checking git..."
if ! command -v git &>/dev/null; then
  error "git is required. Install Xcode Command Line Tools: xcode-select --install"
fi
success "Found $(git --version)"

# ── Check Claude CLI ──────────────────────────────────────────────────────────
info "Checking Claude CLI..."
if command -v claude &>/dev/null; then
  success "Claude CLI found"
else
  warn "Claude CLI not found. Install it from https://claude.ai/download"
  warn "Ensemble will still install but won't be able to run agents until Claude CLI is set up."
fi

# ── Check gh CLI ──────────────────────────────────────────────────────────────
info "Checking GitHub CLI..."
if command -v gh &>/dev/null; then
  success "GitHub CLI found"
else
  warn "GitHub CLI not found. PR creation won't work until you: brew install gh && gh auth login"
fi

echo ""

# ── Download ─────────────────────────────────────────────────────────────────
info "Downloading Ensemble..."

# Back up existing install if present
if [[ -d "$INSTALL_DIR" ]]; then
  warn "Existing install found at $INSTALL_DIR — backing up..."
  BACKUP_DIR="${INSTALL_DIR}_backup_$(date +%Y%m%d_%H%M%S)"
  mv "$INSTALL_DIR" "$BACKUP_DIR"
  info "Backed up to $BACKUP_DIR"
fi

mkdir -p "$HOME/tools"
cd "$HOME/tools"

# Download and extract
TMP_TAR=$(mktemp /tmp/ensemble_XXXXXX.tar.gz)
if command -v curl &>/dev/null; then
  curl -sSL "$RELEASE_URL" -o "$TMP_TAR"
elif command -v wget &>/dev/null; then
  wget -q "$RELEASE_URL" -O "$TMP_TAR"
else
  error "curl or wget is required to download Ensemble"
fi

# GitHub tarballs extract to repo-branch/ so we rename
TMP_DIR=$(mktemp -d)
tar -xzf "$TMP_TAR" -C "$TMP_DIR"
EXTRACTED=$(ls "$TMP_DIR" | head -1)
mv "$TMP_DIR/$EXTRACTED" "$INSTALL_DIR"
rm -f "$TMP_TAR"
rm -rf "$TMP_DIR"

success "Downloaded to $INSTALL_DIR"

# ── Create venv ───────────────────────────────────────────────────────────────
cd "$INSTALL_DIR"

info "Creating Python virtual environment..."
"$PYTHON" -m venv venv
success "Virtual environment created"

# ── Install dependencies ──────────────────────────────────────────────────────
info "Installing dependencies..."
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
./venv/bin/pip install --quiet rumps keyring requests

success "Dependencies installed"

# ── Create shell command ──────────────────────────────────────────────────────
info "Installing 'ensemble' command..."

# Detect shell config file
SHELL_RC=""
if [[ -f "$HOME/.zshrc" ]]; then
  SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then
  SHELL_RC="$HOME/.bashrc"
elif [[ -f "$HOME/.bash_profile" ]]; then
  SHELL_RC="$HOME/.bash_profile"
fi

ALIAS_LINE="alias ensemble='cd $INSTALL_DIR && source venv/bin/activate && python3 ensemble_menubar.py'"
LAUNCH_LINE="alias ensemble-server='cd $INSTALL_DIR && source venv/bin/activate && python3 app.py'"

if [[ -n "$SHELL_RC" ]]; then
  # Remove old aliases if present
  grep -v "alias ensemble=" "$SHELL_RC" > /tmp/shell_rc_tmp && mv /tmp/shell_rc_tmp "$SHELL_RC" || true

  # Add new aliases
  echo "" >> "$SHELL_RC"
  echo "# Ensemble — AI agents that peer-review each other" >> "$SHELL_RC"
  echo "$ALIAS_LINE" >> "$SHELL_RC"
  echo "$LAUNCH_LINE" >> "$SHELL_RC"

  success "Added 'ensemble' command to $SHELL_RC"
else
  warn "Couldn't detect shell config. To add the command manually, add to your shell config:"
  echo "  $ALIAS_LINE"
fi

# ── Create launch script ──────────────────────────────────────────────────────
cat > "$INSTALL_DIR/launch.sh" << 'LAUNCH_EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source venv/bin/activate
python3 ensemble_menubar.py
LAUNCH_EOF
chmod +x "$INSTALL_DIR/launch.sh"

# ── Done! ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  ✅ Ensemble installed successfully!${RESET}"
echo ""
echo "  Next steps:"
echo "  1. Get your Anthropic API key from https://console.anthropic.com"
echo "  2. Run: source ~/.zshrc && ensemble"
echo "     (or restart your terminal, then type: ensemble)"
echo ""
echo "  The menubar app will appear as ⚡ in your menu bar."
echo "  Click it → Set API Key → Pick your repo → Start Server"
echo ""
echo "  To update later, just run this install script again."
echo ""

# ── Auto-launch ───────────────────────────────────────────────────────────────
# Only prompt if running interactively (not piped via curl | bash)
if [[ -t 0 ]]; then
  read -p "  Launch Ensemble now? [Y/n] " -n 1 -r
  echo ""
  LAUNCH=$REPLY
else
  LAUNCH="y"
fi

if [[ ! $LAUNCH =~ ^[Nn]$ ]]; then
  info "Launching Ensemble..."
  cd "$INSTALL_DIR"
  source venv/bin/activate
  python3 ensemble_menubar.py &
  echo ""
  success "Ensemble is running — look for ⚡ in your menu bar!"
fi
