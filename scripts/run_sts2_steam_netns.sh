#!/usr/bin/env bash
set -euo pipefail

APP_ID="${STS2_STEAM_APP_ID:-2868840}"
NETNS="${STS2_NETNS:-sts2-offline}"
STEAM_BIN="${STEAM_BIN:-steam}"
KEEP_NETNS="${STS2_KEEP_NETNS:-0}"
ALLOW_EXISTING_STEAM="${STS2_ALLOW_EXISTING_STEAM:-0}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run through sudo so the script can create a network namespace" >&2
  echo "usage: sudo -E $0 [extra Steam/game args...]" >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-}"
if [[ -z "${RUN_USER}" || "${RUN_USER}" == "root" ]]; then
  echo "error: SUDO_USER is not set; run as sudo from the desktop user" >&2
  exit 1
fi

RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
if [[ -z "${RUN_HOME}" ]]; then
  echo "error: could not resolve home directory for ${RUN_USER}" >&2
  exit 1
fi

if [[ "${ALLOW_EXISTING_STEAM}" != "1" ]] && pgrep -u "${RUN_USER}" -x steam >/dev/null 2>&1; then
  echo "error: Steam is already running for ${RUN_USER}" >&2
  echo "Close Steam first, otherwise steam -applaunch may delegate to the existing client outside the namespace." >&2
  echo "Set STS2_ALLOW_EXISTING_STEAM=1 only if you intentionally want that behavior." >&2
  exit 1
fi

command -v ip >/dev/null 2>&1 || {
  echo "error: ip command not found; install iproute2" >&2
  exit 1
}

command -v sudo >/dev/null 2>&1 || {
  echo "error: sudo command not found" >&2
  exit 1
}

cleanup() {
  if [[ "${KEEP_NETNS}" != "1" ]]; then
    ip netns delete "${NETNS}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if ip netns list | awk '{print $1}' | grep -Fxq "${NETNS}"; then
  echo "error: network namespace already exists: ${NETNS}" >&2
  exit 1
fi

ip netns add "${NETNS}"
ip -n "${NETNS}" link set lo up

echo "Launching Steam app ${APP_ID} in netns '${NETNS}' with loopback only."
echo "Steam must already be configured for Offline Mode for this to work reliably."
echo "Run harness commands in the same namespace with:"
echo "  sudo ip netns exec ${NETNS} sudo -E -H -u ${RUN_USER} <command>"

exec ip netns exec "${NETNS}" sudo -E -H -u "${RUN_USER}" \
  env \
    HOME="${RUN_HOME}" \
    USER="${RUN_USER}" \
    LOGNAME="${RUN_USER}" \
    DISPLAY="${DISPLAY:-}" \
    WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
    XAUTHORITY="${XAUTHORITY:-}" \
    XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}" \
    PULSE_SERVER="${PULSE_SERVER:-}" \
    SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-}" \
    "${STEAM_BIN}" -offline -applaunch "${APP_ID}" "$@"
