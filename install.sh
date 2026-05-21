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

# ── Check & install: Homebrew (macOS only) ───────────────────

if [[ "$OS" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
        ok "Homebrew found"
    else
        info "Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        if [[ -f /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -f /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        ok "Homebrew installed"
    fi
fi

# ── Check & install: git ────────────────────────────────────

if command -v git &>/dev/null; then
    ok "git found"
else
    if [[ "$OS" == "Darwin" ]]; then
        info "Installing Xcode command line tools (includes git)..."
        xcode-select --install 2>/dev/null || true
        echo "  Waiting for Xcode tools install to complete..."
        until command -v git &>/dev/null; do sleep 5; done
        ok "git installed"
    else
        info "Installing git..."
        sudo apt-get update -qq && sudo apt-get install -y -qq git
        ok "git installed"
    fi
fi

# ── Check & install: Python 3.10+ ───────────────────────────

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
    info "Installing Python 3..."
    if [[ "$OS" == "Darwin" ]]; then
        brew install python3
    else
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv
    fi
    PYTHON="python3"
    ver=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    ok "Python $ver installed"
fi

# ── Check & install: GDAL ───────────────────────────────────

if command -v gdalinfo &>/dev/null; then
    gdal_ver=$(gdalinfo --version 2>/dev/null | head -1 | sed 's/GDAL //' | cut -d, -f1)
    ok "GDAL $gdal_ver"
else
    info "Installing GDAL (this may take a few minutes)..."
    if [[ "$OS" == "Darwin" ]]; then
        brew install gdal
    else
        sudo apt-get update -qq && sudo apt-get install -y -qq gdal-bin python3-gdal
    fi
    gdal_ver=$(gdalinfo --version 2>/dev/null | head -1 | sed 's/GDAL //' | cut -d, -f1)
    ok "GDAL $gdal_ver installed"
fi

# ── Check & install: pmtiles CLI ────────────────────────────

if command -v pmtiles &>/dev/null; then
    ok "pmtiles CLI found"
else
    info "Installing pmtiles CLI..."
    if [[ "$OS" == "Darwin" ]]; then
        brew install pmtiles
    else
        # Try go install if go is available, otherwise skip
        if command -v go &>/dev/null; then
            go install github.com/protomaps/go-pmtiles/cmd/pmtiles@latest
        else
            warn "pmtiles CLI not installed (go not found). PMTiles output will be unavailable."
            warn "  Install Go, then: go install github.com/protomaps/go-pmtiles/cmd/pmtiles@latest"
        fi
    fi
    if command -v pmtiles &>/dev/null; then
        ok "pmtiles CLI installed"
    fi
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
HAS_MBUTIL="no"
"$VENV_DIR/bin/python" -c "import mbutil" &>/dev/null && HAS_MBUTIL="yes"
if [[ "$HAS_MBUTIL" == "no" ]]; then
    "$VENV_DIR/bin/pip" install --quiet mbutil
fi

ok "hillgen installed"

# ── Add to PATH ──────────────────────────────────────────────

BIN_DIR="$VENV_DIR/bin"
PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""

# Ensure shell RC file exists
touch "$SHELL_RC"

HAS_PATH="no"
grep -qF "hillshade-generator" "$SHELL_RC" 2>/dev/null && HAS_PATH="yes"

if [[ "$HAS_PATH" == "yes" ]]; then
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
echo -e "  Open a new terminal, then:"
echo ""
echo -e "    ${BOLD}hillgen run --place \"Artist Point, WA\" --theme alpine-glacier --zoom 10-14${NC}"
echo ""
