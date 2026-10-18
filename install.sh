#!/usr/bin/env bash
# chttr installer for any Linux desktop:
#
#   curl -fsSL https://raw.githubusercontent.com/robjobbed/chttr/main/install.sh | bash
#
# Clones chttr to ~/.local/share/chttr/src (re-run this script, or `git pull` there,
# to update), installs it with uv, then runs `chttr setup` (speech models, the
# systemd user service, hotkeys, the Meet extension). It never uses sudo: system
# packages it needs are printed for you to install.
set -euo pipefail

repo="${CHTTR_REPO:-https://github.com/robjobbed/chttr.git}"
src="${XDG_DATA_HOME:-$HOME/.local/share}/chttr/src"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

[[ $(uname -s) == Linux ]] || { echo "chttr runs on Linux only."; exit 1; }

# ---- system packages -----------------------------------------------------------
session="${XDG_SESSION_TYPE:-}"
need=()
command -v pw-record >/dev/null || need+=(pipewire-tools)
command -v notify-send >/dev/null || need+=(libnotify)
command -v git >/dev/null || need+=(git)
if [[ $session == x11 ]]; then
  command -v xdotool >/dev/null || need+=(xdotool)
  command -v xclip >/dev/null || command -v xsel >/dev/null || need+=(xclip)
else
  command -v wl-copy >/dev/null || need+=(wl-clipboard)
  if [[ -n ${HYPRLAND_INSTANCE_SIGNATURE:-}${SWAYSOCK:-}${NIRI_SOCKET:-} ]]; then
    command -v wtype >/dev/null || need+=(wtype)
  else
    command -v ydotool >/dev/null || need+=(ydotool)
  fi
fi

if ((${#need[@]})); then
  if command -v pacman >/dev/null; then
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire/' | tr '\n' ' ')
    cmd="sudo pacman -S --needed $pkgs"
  elif command -v apt-get >/dev/null; then
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire-bin/; s/^libnotify$/libnotify-bin/' | tr '\n' ' ')
    cmd="sudo apt install $pkgs"
  elif command -v dnf >/dev/null; then
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire-utils/' | tr '\n' ' ')
    cmd="sudo dnf install $pkgs"
  elif command -v zypper >/dev/null; then
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire-tools/; s/^libnotify$/libnotify-tools/' | tr '\n' ' ')
    cmd="sudo zypper install $pkgs"
  else
    cmd="(install: ${need[*]})"
  fi
  say "chttr needs a few system tools first:"
  echo "    $cmd"
  if [[ -t 0 ]]; then
    read -rp "Run that now? [y/N] " yes
    [[ $yes == [yY]* ]] && eval "$cmd"
  else
    echo "Run it, then run this installer again."
    exit 1
  fi
fi

# ---- uv ------------------------------------------------------------------------
if ! command -v uv >/dev/null; then
  say "Installing uv (https://astral.sh/uv)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# ---- chttr -----------------------------------------------------------------------
if [[ -d $src/.git ]]; then
  say "Updating chttr in $src…"
  git -C "$src" pull --ff-only --quiet
else
  say "Downloading chttr to $src…"
  mkdir -p "$(dirname "$src")"
  git clone --quiet --depth 1 "$repo" "$src"
fi

# Python: the system's own when it is 3.12 or newer (nothing to download), else a uv-managed 3.13.
sys_py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
py="${CHTTR_PYTHON:-$sys_py}"
[[ $py =~ ^3\.([0-9]+)(\.[0-9]+)?$ ]] && ((BASH_REMATCH[1] >= 12)) || py=3.13
# (bin/setup chooses with the same lines; CHTTR_PYTHON=3.14 picks one yourself)
if [[ -z ${CHTTR_PYTHON:-} && $py != "$sys_py" ]]; then
  if [[ -n $sys_py ]]; then
    echo "Your python3 is $sys_py and chttr needs 3.12 or newer, so uv fetches its own Python 3.13 for chttr."
  else
    echo "No python3 found, so uv fetches its own Python 3.13 for chttr."
  fi
fi
exec "$src/bin/setup" "$@"
