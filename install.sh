#!/usr/bin/env bash
set -euo pipefail

# hillgen installer for macOS/Linux
# Usage: curl -fsSL https://raw.githubusercontent.com/emuehlstein/hillshade-generator/main/install.sh | bash

REPO="https://github.com/emuehlstein/hillshade-generator.git"
INSTALL_DIR="$HOME/hillshade-generator"
VENV_DIR="$INSTALL_DIR/.venv"
SHELL_RC=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}▸${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; }

echo ""
echo -e "${BOLD}  ⛰  hillgen installer${NC}"
echo -e "  Beautiful terrain maps from real-world elevation data"
echo ""

# ── Detect OS ────────────────────────────────────────────────

OS="$(uname -s)"
if [[ "$OS" == "Darwin" ]]; then
    info "Detected macOS"
elif [[ "$OS" == "Linux" ]]; then
    info "Detected Linux"
else
    fail "Unsupported OS: $OS (macOS and Linux only)"
    exit 1
fi

# ── Detect shell config ─────────────────────────────────────

CURRENT_SHELL="$(basename "$SHELL")"
if [[ "$CURRENT_SHELL" == "zsh" ]]; then
    SHELL_RC="$HOME/.zshrc"
elif [[ "$CURRENT_SHELL" == "bash" ]]; then
    if [[ "$OS" == "Darwin" ]]; then
        SHELL_RC="$HOME/.bash_profile"
    else
        SHELL_RC="$HOME/.bashrc"
    fi
else
    SHELL_RC="$HOME/.profile"
fi

# ── Check: Homebrew (macOS only) ─────────────────────────────

if [[ "$OS" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
        ok "Homebrew found"
    else
        fail "Homebrew not found"
        echo ""
        echo "  Install it first:"
        echo -e "  ${BOLD}/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"${NC}"
        echo ""
        exit 1
    fi
fi

# ── Check: Python 3.10+ ─────────────────────────────────────

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [[ -n "$PYTHON" ]]; then
    ok "Python $ver ($PYTHON)"
else
    fail "Python 3.10+ not found"
    echo ""
    if [[ "$OS" == "Darwin" ]]; then
        echo -e "  Install: ${BOLD}brew install python3${NC}"
    else
        echo -e "  Install: ${BOLD}sudo apt install python3 python3-venv${NC}"
    fi
    echo ""
    exit 1
fi

# ── Check: GDAL ─────────────────────────────────────────────

if command -v gdalinfo &>/dev/null; then
    gdal_ver=$(gdalinfo --version 2>/dev/null | head -1 | sed 's/GDAL //' | cut -d, -f1)
    ok "GDAL $gdal_ver"
else
    fail "GDAL not found"
    echo ""
    if [[ "$OS" == "Darwin" ]]; then
        echo -e "  Install: ${BOLD}brew install gdal${NC}"
    else
        echo -e "  Install: ${BOLD}sudo apt install gdal-bin python3-gdal${NC}"
    fi
    echo ""
    exit 1
fi

# ── Check: git ───────────────────────────────────────────────

if command -v git &>/dev/null; then
    ok "git found"
else
    fail "git not found"
    echo ""
    if [[ "$OS" == "Darwin" ]]; then
        echo -e "  Install: ${BOLD}xcode-select --install${NC}"
    else
        echo -e "  Install: ${BOLD}sudo apt install git${NC}"
    fi
    echo ""
    exit 1
fi

# ── Clone or update ──────────────────────────────────────────

echo ""
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing install..."
    cd "$INSTALL_DIR"
    git pull --quiet
    ok "Updated to latest"
else
    info "Cloning hillgen..."
    git clone --quiet "$REPO" "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Create/update venv ───────────────────────────────────────

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

info "Installing hillgen..."
"$VENV_DIR/bin/pip" install --quiet -e .

# Install mbutil if missing
if ! "$VENV_DIR/bin/python" -c "import mbutil" &>/dev/null; then
    "$VENV_DIR/bin/pip" install --quiet mbutil
fi

ok "hillgen installed"

# ── Check: pmtiles CLI (optional) ────────────────────────────

if command -v pmtiles &>/dev/null; then
    ok "pmtiles CLI found"
else
    warn "pmtiles CLI not found (optional — needed for PMTiles output)"
    if [[ "$OS" == "Darwin" ]]; then
        echo -e "    Install: ${BOLD}brew install pmtiles${NC}"
    else
        echo -e "    Install: ${BOLD}go install github.com/protomaps/go-pmtiles/cmd/pmtiles@latest${NC}"
    fi
fi

# ── Add to PATH ──────────────────────────────────────────────

BIN_DIR="$VENV_DIR/bin"
ALIAS_LINE="alias hillgen='$BIN_DIR/hillgen'"
PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""

# Check if already in shell config
if grep -qF "hillshade-generator" "$SHELL_RC" 2>/dev/null; then
    ok "Shell config already set up"
else
    echo "" >> "$SHELL_RC"
    echo "# hillgen" >> "$SHELL_RC"
    echo "$PATH_LINE" >> "$SHELL_RC"
    ok "Added to $SHELL_RC"
fi

# ── Verify ───────────────────────────────────────────────────

echo ""
"$BIN_DIR/hillgen" version
echo ""
echo -e "${GREEN}${BOLD}  ✓ hillgen installed successfully!${NC}"
echo ""
echo -e "  Run ${BOLD}source $SHELL_RC${NC} or open a new terminal, then:"
echo ""
echo -e "    ${BOLD}hillgen run --place \"Mt. St. Helens\" --theme midnight --zoom 10-14${NC}"
echo ""
