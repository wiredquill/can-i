#!/usr/bin/env bash
# can-i one-line bootstrap installer (native / AI-agent payload style).
#
#   curl -fsSL https://raw.githubusercontent.com/wiredquill/can-i/main/install.sh | bash
#
# Sets up mise (if absent), resolves tool dependencies, installs can-i,
# and drops a `can-i` shim into ~/.local/bin.
set -euo pipefail

REPO_URL="${CAN_I_REPO:-https://github.com/wiredquill/can-i.git}"
INSTALL_DIR="${CAN_I_HOME:-$HOME/.local/share/can-i}"
BIN_DIR="$HOME/.local/bin"

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

# 1. mise ----------------------------------------------------------------------
if ! command -v mise >/dev/null 2>&1; then
  info "mise not found — installing"
  curl -fsSL https://mise.run | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
eval "$(mise activate bash)" 2>/dev/null || true

# 2. source ----------------------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  info "updating existing checkout in $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  info "cloning $REPO_URL -> $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# 3. toolchains + dependencies --------------------------------------------------
info "installing runtimes via mise (.mise.toml)"
mise trust --yes 2>/dev/null || mise trust || true
mise install
info "installing can-i and dependencies"
mise run install

# 4. CLI shim ---------------------------------------------------------------------
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/can-i" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/.venv/bin/can-i" "\$@"
EOF
chmod +x "$BIN_DIR/can-i"

info "done. Try:"
echo "    can-i run --config $INSTALL_DIR/checks.yaml"
echo "    can-i tui --config $INSTALL_DIR/checks.yaml"
echo "    can-i web --config $INSTALL_DIR/checks.yaml --port 8000"
