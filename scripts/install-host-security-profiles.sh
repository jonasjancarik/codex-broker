#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Install and preflight the host-side profiles used by the managed Docker
# deployment. The checked-in profiles are the source of truth; this script
# never downloads or generates a profile during installation.

set -eu

usage() {
  cat >&2 <<'EOF'
Usage: install-host-security-profiles.sh [--check | --dry-run]

  (default)  Install the seccomp profile and load the AppArmor profile.
  --check    Run a non-mutating preflight against sources and installed files.
  --dry-run  Validate sources and print the installation actions.

CODEX_BROKER_SECURITY_ROOT may be set for a test/staged destination.
CODEX_BROKER_APPARMOR_PATH may be set for a test/staged AppArmor file.
EOF
}

MODE=install
case "${1:-}" in
  "") ;;
  --check) MODE=check ;;
  --dry-run) MODE=dry-run ;;
  --help|-h) usage ; exit 0 ;;
  *) usage ; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  usage
  exit 2
fi

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd -P)
SECCOMP_SOURCE=$REPO_ROOT/examples/seccomp/codex-broker.json
APPARMOR_SOURCE=$REPO_ROOT/examples/apparmor/codex-broker-bwrap
SECURITY_ROOT=${CODEX_BROKER_SECURITY_ROOT:-/etc/codex-broker/security}
SECCOMP_DEST=$SECURITY_ROOT/v1/seccomp.json
APPARMOR_DEST=${CODEX_BROKER_APPARMOR_PATH:-/etc/apparmor.d/codex-broker-bwrap}

MOBY_SECCOMP_TAG=seccomp/v0.2.3
MOBY_SECCOMP_COMMIT=f1a0fd6b5a369fca061b041539129661ed337ef5
MOBY_SECCOMP_SHA256=536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74
MOBY_APPARMOR_TAG=apparmor/v0.2.1
MOBY_APPARMOR_COMMIT=8f67eb80c7c8cdf6eb8f72e776c3864e9509f4d2
MOBY_APPARMOR_SHA256=2e9f9780a9b64d4f547f8563399d1147b22d810d973701688b5eb4cb0ec46168

error() {
  printf '%s\n' "error: $*" >&2
}

info() {
  printf '%s\n' "$*"
}

file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    error "sha256sum or shasum is required"
    return 1
  fi
}

validate_json() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -m json.tool "$SECCOMP_SOURCE" >/dev/null
  elif command -v jq >/dev/null 2>&1; then
    jq -e . "$SECCOMP_SOURCE" >/dev/null
  else
    error "python3 or jq is required to validate the seccomp profile"
    return 1
  fi
}

validate_sources() {
  [ -r "$SECCOMP_SOURCE" ] || { error "missing seccomp source: $SECCOMP_SOURCE"; return 1; }
  [ -r "$APPARMOR_SOURCE" ] || { error "missing AppArmor source: $APPARMOR_SOURCE"; return 1; }
  validate_json || { error "invalid JSON: $SECCOMP_SOURCE"; return 1; }
  grep -Fq "Moby $MOBY_SECCOMP_TAG baseline" "$SECCOMP_SOURCE" || {
    error "seccomp profile is missing Moby baseline provenance"
    return 1
  }
  grep -Fq "$MOBY_SECCOMP_COMMIT" "$SECCOMP_SOURCE" || {
    error "seccomp profile is missing Moby commit provenance"
    return 1
  }
  grep -Fq "$MOBY_SECCOMP_SHA256" "$SECCOMP_SOURCE" || {
    error "seccomp profile is missing Moby checksum provenance"
    return 1
  }
  grep -Fq "$MOBY_APPARMOR_TAG" "$APPARMOR_SOURCE" || {
    error "AppArmor profile is missing Moby commit provenance"
    return 1
  }
  grep -Fq "$MOBY_APPARMOR_COMMIT" "$APPARMOR_SOURCE" || {
    error "AppArmor profile is missing Moby commit provenance"
    return 1
  }
  grep -Fq "$MOBY_APPARMOR_SHA256" "$APPARMOR_SOURCE" || {
    error "AppArmor profile is missing Moby checksum provenance"
    return 1
  }
  grep -Fq "deny network alg" "$APPARMOR_SOURCE" || {
    error "AppArmor profile does not deny AF_ALG"
    return 1
  }
}

apparmor_parser_path() {
  command -v apparmor_parser 2>/dev/null || true
}

apparmor_enabled() {
  enabled_parameter=/sys/module/apparmor/parameters/enabled
  if [ -r "$enabled_parameter" ]; then
    case $(cat "$enabled_parameter") in
      Y|y|yes|Yes|YES|1) return 0 ;;
      *) return 1 ;;
    esac
  fi

  # A loaded AppArmor module without a readable parameter is treated as
  # enabled. Do not silently skip loaded-profile verification just because the
  # caller lacks access to securityfs.
  [ -d /sys/module/apparmor ]
}

require_loaded_profile_access() {
  if [ ! -r /sys/kernel/security/apparmor/profiles ]; then
    error "AppArmor is enabled, but loaded profiles cannot be inspected; rerun --check with sudo"
    return 1
  fi
}

validate_apparmor() {
  parser=$(apparmor_parser_path)
  if [ -z "$parser" ]; then
    error "apparmor_parser is required to validate the AppArmor profile"
    return 1
  fi
  # -Q parses without loading into the kernel; -W keeps warnings visible.
  "$parser" -Q -W "$APPARMOR_SOURCE" >/dev/null
}

installed_file_matches() {
  [ -f "$2" ] || return 1
  [ "$(file_sha256 "$1")" = "$(file_sha256 "$2")" ]
}

file_has_root_0644() {
  [ -f "$1" ] || return 1
  if metadata=$(stat -c '%a %U:%G' "$1" 2>/dev/null); then
    :
  else
    metadata=$(stat -f '%Lp %Su:%Sg' "$1")
  fi
  [ "$metadata" = "644 root:root" ]
}

check_installed() {
  status=0
  if installed_file_matches "$SECCOMP_SOURCE" "$SECCOMP_DEST"; then
    if file_has_root_0644 "$SECCOMP_DEST"; then
      info "ok: seccomp installed at $SECCOMP_DEST (root:root, 0644)"
    else
      error "seccomp has incorrect owner or mode (expected root:root, 0644): $SECCOMP_DEST"
      status=1
    fi
  else
    error "seccomp is missing or differs from $SECCOMP_SOURCE: $SECCOMP_DEST"
    status=1
  fi
  if ! apparmor_enabled; then
    info "skip: AppArmor is unavailable or disabled on this host"
    return "$status"
  fi
  if installed_file_matches "$APPARMOR_SOURCE" "$APPARMOR_DEST"; then
    if file_has_root_0644 "$APPARMOR_DEST"; then
      info "ok: AppArmor profile installed at $APPARMOR_DEST (root:root, 0644)"
    else
      error "AppArmor profile has incorrect owner or mode (expected root:root, 0644): $APPARMOR_DEST"
      status=1
    fi
  else
    error "AppArmor profile is missing or differs from $APPARMOR_SOURCE: $APPARMOR_DEST"
    status=1
  fi
  if ! require_loaded_profile_access; then
    return 1
  fi
  if grep -Fqx "codex-broker-bwrap (enforce)" /sys/kernel/security/apparmor/profiles \
    || grep -Fqx "codex-broker-bwrap (complain)" /sys/kernel/security/apparmor/profiles; then
    info "ok: AppArmor profile codex-broker-bwrap is loaded"
  else
    error "AppArmor profile codex-broker-bwrap is not loaded"
    status=1
  fi
  return "$status"
}

validate_sources

case "$MODE" in
  check)
    if apparmor_enabled; then
      validate_apparmor
    else
      info "skip: AppArmor syntax check (AppArmor is unavailable or disabled)"
    fi
    check_installed
    ;;
  dry-run)
    if apparmor_enabled; then
      validate_apparmor
    else
      info "skip: AppArmor syntax check (AppArmor is unavailable or disabled)"
    fi
    info "source seccomp: $SECCOMP_SOURCE"
    info "destination seccomp: $SECCOMP_DEST"
    info "source AppArmor: $APPARMOR_SOURCE"
    info "destination AppArmor: $APPARMOR_DEST"
    info "would create $SECURITY_ROOT/v1 with mode 0755"
    info "would install seccomp profile with mode 0644"
    info "would install AppArmor profile with mode 0644"
    info "would load AppArmor profile with apparmor_parser -r -W"
    ;;
  install)
    if [ "$(id -u)" -ne 0 ]; then
      error "installation requires root; rerun with sudo"
      exit 1
    fi
    install -d -m 0755 "$SECURITY_ROOT/v1"
    if installed_file_matches "$SECCOMP_SOURCE" "$SECCOMP_DEST"; then
      info "unchanged: $SECCOMP_DEST"
    else
      install -m 0644 "$SECCOMP_SOURCE" "$SECCOMP_DEST"
      info "installed: $SECCOMP_DEST"
    fi
    chmod 0644 "$SECCOMP_DEST"
    chown root:root "$SECCOMP_DEST"
    if ! apparmor_enabled; then
      info "skip: AppArmor is unavailable or disabled on this host"
      exit 0
    fi
    install -d -m 0755 "$(dirname -- "$APPARMOR_DEST")"
    if installed_file_matches "$APPARMOR_SOURCE" "$APPARMOR_DEST"; then
      info "unchanged: $APPARMOR_DEST"
    else
      install -m 0644 "$APPARMOR_SOURCE" "$APPARMOR_DEST"
      info "installed: $APPARMOR_DEST"
    fi
    chmod 0644 "$APPARMOR_DEST"
    chown root:root "$APPARMOR_DEST"
    parser=$(apparmor_parser_path)
    if [ -z "$parser" ]; then
      error "apparmor_parser is required to load the AppArmor profile"
      exit 1
    fi
    "$parser" -r -W "$APPARMOR_DEST"
    info "loaded: codex-broker-bwrap"
    check_installed
    ;;
esac
