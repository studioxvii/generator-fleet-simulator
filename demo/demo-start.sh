#!/usr/bin/env bash
# Studio Seventeen — Simulator Suite Demo Launcher
# Starts Generator Fleet, BESS, and PV simulators in one command.

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
BOLD='\033[1m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RESET='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_header() {
  echo -e "${CYAN}${BOLD}"
  echo "  ┌─────────────────────────────────────────────────────┐"
  echo "  │         Studio Seventeen — Simulator Suite          │"
  echo "  │     Generator Fleet  ·  BESS  ·  PV Simulator      │"
  echo "  └─────────────────────────────────────────────────────┘"
  echo -e "${RESET}"
}

print_step() {
  echo -e "${CYAN}▶${RESET} ${BOLD}$1${RESET}"
}

print_ok() {
  echo -e "  ${GREEN}✓${RESET} $1"
}

print_warn() {
  echo -e "  ${YELLOW}⚠${RESET}  $1"
}

print_error() {
  echo -e "  ${RED}✗${RESET} $1"
}

# ── Prerequisite checks ───────────────────────────────────────────────────────
check_prerequisites() {
  print_step "Checking prerequisites"

  local missing=0

  if command -v docker &>/dev/null; then
    local docker_version
    docker_version=$(docker --version 2>&1 | head -1)
    print_ok "Docker: ${docker_version}"
  else
    print_error "Docker is not installed. Install Docker Desktop: https://docs.docker.com/get-docker/"
    missing=1
  fi

  if docker compose version &>/dev/null 2>&1; then
    local compose_version
    compose_version=$(docker compose version 2>&1 | head -1)
    print_ok "Docker Compose: ${compose_version}"
  elif command -v docker-compose &>/dev/null; then
    local compose_version
    compose_version=$(docker-compose --version 2>&1 | head -1)
    print_ok "Docker Compose (standalone): ${compose_version}"
    # Alias for rest of script
    docker() { if [ "$1" = "compose" ]; then shift; docker-compose "$@"; else command docker "$@"; fi; }
    export -f docker 2>/dev/null || true
  else
    print_error "Docker Compose is not installed. It is included with Docker Desktop."
    missing=1
  fi

  if docker info &>/dev/null 2>&1; then
    print_ok "Docker daemon is running"
  else
    print_error "Docker daemon is not running. Start Docker Desktop and try again."
    missing=1
  fi

  if [ "$missing" -ne 0 ]; then
    echo ""
    echo -e "${RED}${BOLD}One or more prerequisites are missing. Cannot continue.${RESET}"
    exit 1
  fi

  echo ""
}

# Show the MIT license without a startup acceptance gate.
check_license() {
  print_step "MIT License"
  cat "$SCRIPT_DIR/../LICENSE"
  echo "MIT applies to Generator Fleet project code only."
  echo "Review BESS and PV licenses separately; their acceptance settings are unchanged."
}


# ── Network binding ───────────────────────────────────────────────────────────
confirm_host_binding() {
  export SIM_HOST_BIND="${SIM_HOST_BIND:-127.0.0.1}"

  case "$SIM_HOST_BIND" in
    127.0.0.1|localhost|::1)
      print_ok "Demo host ports will bind to ${SIM_HOST_BIND}"
      echo ""
      return
      ;;
  esac

  print_warn "Demo host ports will bind to ${SIM_HOST_BIND}"
  echo "  Dashboards and unauthenticated Modbus TCP ports may be reachable from other machines."
  echo "  Use this only on an isolated engineering or test network."
  echo ""

  local confirm=""
  while [ "$confirm" != "trusted-lan" ]; do
    echo -e "${BOLD}Type trusted-lan to confirm LAN exposure:${RESET}"
    read -r confirm
  done

  echo ""
}

# ── Pull latest images ────────────────────────────────────────────────────────
pull_images() {
  print_step "Pulling latest simulator images"
  echo ""

  cd "$SCRIPT_DIR"

  if docker compose pull 2>&1 | while IFS= read -r line; do echo "  $line"; done; then
    print_ok "All images pulled"
  else
    print_warn "Image pull failed — will use cached images if available"
  fi

  echo ""
}

# ── Start simulators ──────────────────────────────────────────────────────────
start_simulators() {
  print_step "Starting simulators"
  echo ""

  cd "$SCRIPT_DIR"

  SIM_HOST_BIND="$SIM_HOST_BIND" docker compose up -d

  echo ""
}

# ── Wait for health ───────────────────────────────────────────────────────────
wait_for_health() {
  print_step "Waiting for simulators to become ready"
  echo ""

  local services=("generator-fleet-sim:5001:/api/state" "bess-sim:5002:/api/health" "pv-sim:5003:/api/health")
  local max_wait=60
  local all_up=1

  for entry in "${services[@]}"; do
    IFS=':' read -r name port path <<< "$entry"
    local waited=0
    printf "  Waiting for %-24s" "${name}..."

    while true; do
      if curl -sf --max-time 3 "http://localhost:${port}${path}" &>/dev/null 2>&1; then
        echo -e " ${GREEN}✓ up${RESET}"
        break
      fi

      waited=$((waited + 2))
      if [ "$waited" -ge "$max_wait" ]; then
        echo -e " ${RED}✗ timeout${RESET}"
        all_up=0
        break
      fi

      sleep 2
      printf "."
    done
  done

  echo ""

  if [ "$all_up" -eq 0 ]; then
    print_warn "Some simulators did not become healthy in time."
    echo "  Check logs with: docker compose logs -f  (from the demo/ directory)"
    echo ""
  fi
}

# ── Print access info ─────────────────────────────────────────────────────────
print_access_info() {
  echo -e "${GREEN}${BOLD}All simulators are running!${RESET}"
  echo ""
  echo -e "  ${BOLD}Dashboards${RESET}"
  echo -e "  ${CYAN}Generator Fleet${RESET}  →  http://${SIM_HOST_BIND}:5001"
  echo -e "  ${CYAN}BESS Simulator  ${RESET}  →  http://${SIM_HOST_BIND}:5002"
  echo -e "  ${CYAN}PV Simulator    ${RESET}  →  http://${SIM_HOST_BIND}:5003"
  echo ""
  echo -e "  ${BOLD}Modbus TCP endpoints${RESET}"
  echo "  Generator Fleet  →  ${SIM_HOST_BIND}:5030-5037"
  echo "  BESS Simulator   →  ${SIM_HOST_BIND}:5022"
  echo "  PV Simulator     →  ${SIM_HOST_BIND}:5023"
  echo ""
  echo -e "  ${BOLD}Useful commands${RESET} (run from the demo/ directory)"
  echo "  docker compose logs -f          # tail all logs"
  echo "  docker compose logs generator   # logs for one service"
  echo "  docker compose stop             # stop all (preserves data)"
  echo "  docker compose down -v          # stop + delete all data"
  echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  print_header
  check_prerequisites
  check_license
  confirm_host_binding
  pull_images
  start_simulators
  wait_for_health
  print_access_info
}

main "$@"
