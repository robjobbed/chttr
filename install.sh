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
command -v pw-record >/dev/null && command -v pw-dump >/dev/null || need+=(pipewire-tools)
command -v pactl >/dev/null || need+=(pactl)
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
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire/; s/^pactl$/libpulse/' | tr '\n' ' ')
    cmd="sudo pacman -S --needed $pkgs"
  elif command -v apt-get >/dev/null; then
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire-bin/; s/^libnotify$/libnotify-bin/; s/^pactl$/pulseaudio-utils/' | tr '\n' ' ')
    cmd="sudo apt install $pkgs"
  elif command -v dnf >/dev/null; then
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire-utils/; s/^pactl$/pulseaudio-utils/' | tr '\n' ' ')
    cmd="sudo dnf install $pkgs"
  elif command -v zypper >/dev/null; then
    pkgs=$(printf '%s\n' "${need[@]}" | sed 's/^pipewire-tools$/pipewire-tools/; s/^libnotify$/libnotify-tools/; s/^pactl$/pulseaudio-utils/' | tr '\n' ' ')
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

# The waveform overlay is optional (without it chttr shows notifications instead).
# GNOME and X11 have no layer-shell, so the packages would do nothing there.
py=/usr/bin/python3; [[ -x $py ]] || py=python3
desk="${XDG_CURRENT_DESKTOP:-}"
if [[ $session != x11 && ${desk,,} != *gnome* ]] && ! "$py" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Gtk4LayerShell','1.0')" 2>/dev/null; then
  echo "recommended for the waveform overlay: python-gobject gtk4-layer-shell (Debian: python3-gi gir1.2-gtk-4.0 gir1.2-gtk4layershell-1.0)"
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

export CHTTR_PYTHON="${CHTTR_PYTHON:-3.13}"
exec "$src/bin/setup" "$@"
