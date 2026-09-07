#!/usr/bin/env bash
# Studio Seventeen guided launcher for Generator Fleet Simulator Community Edition.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
STATE_DIR="$SCRIPT_DIR/.studioseventeen"
SECURITY_REVIEW_FILE="$STATE_DIR/security-reviewed"
NETWORK_MODE_FILE="$STATE_DIR/network-mode"
VERSION_FILE="$SCRIPT_DIR/VERSION"
IMAGE_REPOSITORY="studioxvii/generator-fleet-sim"
IMAGE_TAG="$(sed -n '1p' "$VERSION_FILE" 2>/dev/null || true)"
IMAGE_TAG="${IMAGE_TAG:-1.1.0-rc.4}"
FLEET_STARTUP_PAYLOAD=""
FLEET_TOTAL=0
FLEET_CONFIG_DESCRIPTION="Configure later in browser"
DOCKER_CHECK_ERROR=""

print_header() {
  printf "\n"
  printf "Studio Seventeen - Generator Fleet Simulator Community Edition\n"
  printf "Guided startup\n"
  printf "\n"
}

pause() {
  printf "\nPress Enter to continue..."
  IFS= read -r _
}

read_file() {
  local file="$1"
  if [ ! -f "$file" ]; then
    printf "Missing file: %s\n" "$file"
    return 1
  fi

  printf -- "-------------------------------------------------------------------------------\n"
  sed -n '1,260p' "$file"
  printf -- "-------------------------------------------------------------------------------\n"
}

record_acknowledgement() {
  local file="$1"
  mkdir -p "$STATE_DIR"
  {
    printf "accepted_at=%s\n" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    printf "launcher=%s\n" "$(basename "$0")"
  } > "$file"
}

network_mode() {
  if [ -f "$NETWORK_MODE_FILE" ] && [ "$(cat "$NETWORK_MODE_FILE")" = "lan" ]; then
    printf "lan"
  else
    printf "local"
  fi
}

host_bind() {
  if [ "$(network_mode)" = "lan" ]; then
    printf "0.0.0.0"
  else
    printf "127.0.0.1"
  fi
}

network_label() {
  if [ "$(network_mode)" = "lan" ]; then
    printf "trusted LAN integration mode enabled"
  else
    printf "local machine only"
  fi
}

compose() {
  docker compose --file "$COMPOSE_FILE" "$@"
}

check_launcher_prerequisites() {
  local missing=0
  if [ -f "$SCRIPT_DIR/RELEASE_RECEIPT.json" ] && ! command -v python3 >/dev/null 2>&1; then
    printf "Python 3 is required on this computer to verify the release receipt. Install python3 before running setup.\n" >&2
    missing=1
  fi
  if ! command -v curl >/dev/null 2>&1; then
    printf "curl is required on this computer to configure the fleet and check readiness. Install curl before running setup.\n" >&2
    missing=1
  fi
  if [ "$missing" -ne 0 ]; then
    printf "See INSTALL.md, Prerequisites. No container has been started.\n" >&2
    return 1
  fi
}

verify_release_integrity() {
  local receipt="$SCRIPT_DIR/RELEASE_RECEIPT.json"
  if [ ! -f "$receipt" ]; then
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    printf "RELEASE_RECEIPT.json is present but python3 is required to verify it.\n" >&2
    return 1
  fi

  python3 - "$receipt" "$VERSION_FILE" "$COMPOSE_FILE" <<'PY'
import json
import pathlib
import sys

receipt_path, version_path, compose_path = map(pathlib.Path, sys.argv[1:])
try:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"RELEASE_RECEIPT.json is not valid JSON: {exc}") from exc
if not isinstance(receipt, dict):
    raise SystemExit("RELEASE_RECEIPT.json must be a JSON object.")
version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
if receipt.get("version") != version:
    raise SystemExit(
        f"Release receipt version {receipt.get('version')!r} does not match VERSION {version!r}."
    )
digest = ((receipt.get("image") or {}).get("digest") if isinstance(receipt.get("image"), dict) else None)
compose = compose_path.read_text(encoding="utf-8")
if digest and f"@{digest}" not in compose and "GENSIM_IMAGE_TAG" not in compose:
    raise SystemExit("docker-compose.yml is not pinned to the receipt image digest.")
print("Release receipt verified.")
PY
}

generator_image_reference() {
  local images
  if ! images="$(compose config --images 2>/dev/null)"; then
    return 0
  fi
  printf "%s\n" "$images" | sed -n '1p'
}

generator_image_available() {
  local image_reference="$1"
  [ -n "$image_reference" ] && docker image inspect "$image_reference" >/dev/null 2>&1
}

docker_engine_ready() {
  local error_file
  error_file="$(mktemp "${TMPDIR:-/tmp}/generator-docker-check.XXXXXX")"
  DOCKER_CHECK_ERROR=""
  docker info >/dev/null 2>"$error_file" &
  local pid=$!
  local waited=0

  while kill -0 "$pid" >/dev/null 2>&1; do
    if [ "$waited" -ge 20 ]; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
      DOCKER_CHECK_ERROR="Docker did not respond within 20 seconds."
      rm -f "$error_file"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  local status=0
  wait "$pid" >/dev/null 2>&1 || status=$?
  DOCKER_CHECK_ERROR="$(cat "$error_file")"
  rm -f "$error_file"
  return "$status"
}

show_docker_install_help() {
  printf "\nDocker Desktop is required before the simulator can start.\n\n"
  printf "Install Docker Desktop:\n"
  printf "  https://docs.docker.com/get-docker/\n\n"

  case "$(uname -s 2>/dev/null || printf unknown)" in
    Darwin)
      printf "macOS option with Homebrew:\n"
      printf "  brew install --cask docker\n\n"
      if command -v brew >/dev/null 2>&1; then
        printf "Type install to run the Homebrew install now, quit to exit, or press Enter after installing manually: "
        IFS= read -r response
        if [ "$response" = "quit" ]; then
          exit 0
        fi
        if [ "$response" = "install" ]; then
          brew install --cask docker || true
        fi
      else
        printf "After installing Docker Desktop, open it once and wait for it to finish starting.\n"
        printf "Type quit to exit, or press Enter after installing manually: "
        IFS= read -r response
        if [ "$response" = "quit" ]; then
          exit 0
        fi
      fi
      ;;
    Linux)
      printf "Linux install instructions vary by distribution. Use Docker's official guide:\n"
      printf "  https://docs.docker.com/engine/install/\n"
      printf "Type quit to exit, or press Enter after installing manually: "
      IFS= read -r response
      if [ "$response" = "quit" ]; then
        exit 0
      fi
      ;;
    *)
      printf "Type quit to exit, or press Enter after installing manually: "
      IFS= read -r response
      if [ "$response" = "quit" ]; then
        exit 0
      fi
      ;;
  esac
}

ensure_docker_installed() {
  while ! command -v docker >/dev/null 2>&1; do
    show_docker_install_help
  done
}

ensure_docker_running() {
  while ! docker_engine_ready; do
    printf "\nCannot access the Docker engine.\n%s\n" "$DOCKER_CHECK_ERROR"
    case "$DOCKER_CHECK_ERROR" in
      *[Pp]ermission\ denied*|*[Aa]ccess\ is\ denied*)
        printf "This account does not have permission to access Docker.\n"
        printf "See INSTALL.md, Docker access denied. Resolve access for this account, then retry.\n"
        printf "Type quit to exit, or press Enter to check again: "
        IFS= read -r response
        if [ "$response" = "quit" ]; then exit 0; fi
        continue
        ;;
    esac
    printf "Start Docker Desktop or the Docker daemon if stopped. If it is already running,\n"
    printf "check docker context show and docker info in this terminal.\n"
    case "$(uname -s 2>/dev/null || printf unknown)" in
      Darwin)
        printf "Type open to launch Docker Desktop, quit to exit, or press Enter to check again: "
        IFS= read -r response
        if [ "$response" = "quit" ]; then
          exit 0
        fi
        if [ "$response" = "open" ]; then
          open -a Docker || true
        fi
        ;;
      *)
        printf "Start Docker Desktop or the Docker daemon, then return here.\n"
        printf "Type quit to exit, or press Enter to check again: "
        IFS= read -r response
        if [ "$response" = "quit" ]; then
          exit 0
        fi
        ;;
    esac
  done
}

ensure_compose_available() {
  while ! docker compose version >/dev/null 2>&1; do
    printf "\nDocker is available, but Docker Compose v2 was not found.\n"
    printf "Install or update Docker Desktop, then return here.\n"
    printf "Type quit to exit, or press Enter after updating Docker Desktop: "
    IFS= read -r response
    if [ "$response" = "quit" ]; then
      exit 0
    fi
  done
}

step_docker() {
  printf "\nStep 1 of 6 - Docker check\n"
  ensure_docker_installed
  ensure_docker_running
  ensure_compose_available
  printf "Docker is ready.\n"
}

step_license() {
  printf "\nStep 2 of 6 - MIT License\n"
  read_file "$SCRIPT_DIR/LICENSE"
}

step_security() {
  printf "\nStep 3 of 6 - Security notes\n"
  printf "Read the security notes below. The simulator cannot launch until you acknowledge them.\n\n"
  read_file "$SCRIPT_DIR/SECURITY.md"

  local response=""
  while [ "$response" != "understood" ]; do
    printf "\nType understood to acknowledge the security notes: "
    IFS= read -r response
  done
  record_acknowledgement "$SECURITY_REVIEW_FILE"
  printf "Security review recorded.\n"
}

step_network() {
  printf "\nStep 4 of 6 - Network exposure\n"
  printf "Modbus TCP is unauthenticated control traffic.\n"
  printf "The dashboard APIs can also issue simulator commands.\n"
  printf "Choose how this computer should publish the simulator ports.\n\n"
  printf "1) Local-only, bound to 127.0.0.1\n"
  printf "2) Trusted LAN integration mode, bound to all host interfaces\n\n"

  local choice=""
  while [ "$choice" != "1" ] && [ "$choice" != "2" ]; do
    printf "Choose 1 or 2: "
    IFS= read -r choice
  done

  mkdir -p "$STATE_DIR"
  if [ "$choice" = "2" ]; then
    local confirm=""
    printf "\nLAN integration mode is for isolated engineering or test networks only.\n"
    printf "Do not use it on public, guest, or general corporate networks without firewall/VPN controls.\n"
    while [ "$confirm" != "trusted-lan" ]; do
      printf "Type trusted-lan to confirm unauthenticated LAN exposure: "
      IFS= read -r confirm
    done
    printf "lan\n" > "$NETWORK_MODE_FILE"
  else
    printf "local\n" > "$NETWORK_MODE_FILE"
  fi

  printf "Network mode set to: %s\n" "$(network_label)"
}

is_nonnegative_integer() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

read_generator_count() {
  local label="$1"
  local value=""
  while true; do
    printf "%s: " "$label" >&2
    IFS= read -r value
    value="${value:-0}"
    if is_nonnegative_integer "$value"; then
      printf "%s" "$value"
      return 0
    fi
    printf "Enter a whole number 0 or greater.\n" >&2
  done
}

set_fleet_payload() {
  local count_500=$((10#$1))
  local count_1000=$((10#$2))
  local count_1500=$((10#$3))
  local count_2000=$((10#$4))
  local count_2500=$((10#$5))
  FLEET_TOTAL=$((count_500 + count_1000 + count_1500 + count_2000 + count_2500))
  FLEET_STARTUP_PAYLOAD=$(printf '{"size_counts":{"500":%s,"1000":%s,"1500":%s,"2000":%s,"2500":%s}}' \
    "$count_500" "$count_1000" "$count_1500" "$count_2000" "$count_2500")
  FLEET_CONFIG_DESCRIPTION=$(printf "%s generators (%s x 500 kW, %s x 1000 kW, %s x 1500 kW, %s x 2000 kW, %s x 2500 kW)" \
    "$FLEET_TOTAL" "$count_500" "$count_1000" "$count_1500" "$count_2000" "$count_2500")
}

step_fleet_config() {
  printf "\nStep 5 of 6 - Fleet configuration\n"
  printf "Choose the fleet mix the launcher should start now.\n\n"
  printf "1) Start default fleet: 15 x 500 kW generators\n"
  printf "2) Enter custom generator counts by size\n"
  printf "3) Configure later in the browser dashboard\n\n"

  local choice=""
  while [ "$choice" != "1" ] && [ "$choice" != "2" ] && [ "$choice" != "3" ]; do
    printf "Choose 1, 2, or 3: "
    IFS= read -r choice
  done

  if [ "$choice" = "1" ]; then
    set_fleet_payload 15 0 0 0 0
  elif [ "$choice" = "2" ]; then
    local count_500 count_1000 count_1500 count_2000 count_2500 total
    while true; do
      count_500="$(read_generator_count "500 kW generators")"
      count_1000="$(read_generator_count "1000 kW generators")"
      count_1500="$(read_generator_count "1500 kW generators")"
      count_2000="$(read_generator_count "2000 kW generators")"
      count_2500="$(read_generator_count "2500 kW generators")"
      total=$((10#$count_500 + 10#$count_1000 + 10#$count_1500 + 10#$count_2000 + 10#$count_2500))
      if [ "$total" -ge 1 ] && [ "$total" -le 2000 ]; then
        set_fleet_payload "$count_500" "$count_1000" "$count_1500" "$count_2000" "$count_2500"
        break
      fi
      printf "Total generators must be between 1 and 2000. Current total: %s\n" "$total"
    done
  else
    FLEET_STARTUP_PAYLOAD=""
    FLEET_TOTAL=0
    FLEET_CONFIG_DESCRIPTION="Configure later in browser"
  fi

  printf "Fleet configuration: %s\n" "$FLEET_CONFIG_DESCRIPTION"
}

wait_for_url() {
  local name="$1"
  local url="$2"
  local max_attempts=30
  local attempt=1

  printf "Waiting for %s" "$name"
  while [ "$attempt" -le "$max_attempts" ]; do
    if command -v curl >/dev/null 2>&1 && curl -fs --max-time 3 "$url" >/dev/null 2>&1; then
      printf " ready\n"
      return 0
    fi
    printf "."
    sleep 2
    attempt=$((attempt + 1))
  done

  printf " timed out\n"
  return 1
}

show_access_info() {
  printf "\nGenerator Fleet Simulator is ready.\n\n"
  printf "Dashboard:\n"
  printf "  http://localhost:5001\n\n"
  printf "Modbus TCP ports (255 generators per port):\n"
  printf "  localhost:5021-5028\n\n"
  printf "Network mode:\n"
  printf "  %s\n\n" "$(network_label)"
  printf "Fleet configuration:\n"
  printf "  %s\n\n" "$FLEET_CONFIG_DESCRIPTION"

  if [ "$(network_mode)" = "lan" ]; then
    printf "Trusted LAN integration mode is enabled. Other devices can connect to this host's IP address.\n"
    printf "Keep this host on an isolated engineering network or behind customer-managed firewall/VPN controls.\n"
  else
    printf "Local-only mode is enabled. Other devices cannot connect through the host ports.\n"
  fi
}

configure_fleet_from_launcher() {
  if [ -z "$FLEET_STARTUP_PAYLOAD" ]; then
    printf "\nFleet startup skipped. Configure the simulator from the browser dashboard.\n"
    return 0
  fi

  printf "Starting configured fleet from terminal"
  local max_attempts=15
  local attempt=1
  local response_file
  response_file="$(mktemp "${TMPDIR:-/tmp}/generator-startup.XXXXXX")"

  while [ "$attempt" -le "$max_attempts" ]; do
    if curl -fsS --max-time 5 \
      -H "Content-Type: application/json" \
      -d "$FLEET_STARTUP_PAYLOAD" \
      "http://localhost:5001/api/startup" > "$response_file"; then
      printf " started\n"
      rm -f "$response_file"
      return 0
    fi
    printf "."
    sleep 2
    attempt=$((attempt + 1))
  done

  printf " failed\n"
  printf "Could not configure the fleet from the terminal. Response, if any:\n"
  sed -n '1,20p' "$response_file" 2>/dev/null || true
  rm -f "$response_file"
  return 1
}

ensure_generator_image() {
  local image_reference
  image_reference="$(generator_image_reference)"

  while true; do
    if compose pull generator; then
      return 0
    fi

    if generator_image_available "$image_reference"; then
      printf "\nCould not refresh the public Generator Fleet image, but the exact pinned image is cached locally.\n"
      printf "A refresh failure can indicate a network outage or a Studio Seventeen publication problem.\n"
      printf "Type use to launch the cached image, retry to pull again, or quit to exit setup: "
      IFS= read -r response
      case "$response" in
        use)
          printf "Using cached Generator Fleet image.\n"
          return 0
          ;;
        retry|"")
          ;;
        quit)
          return 1
          ;;
        *)
          printf "Unknown option.\n"
          ;;
      esac
    else
      printf "\nCould not pull the public Generator Fleet image, and no cached image is available.\n"
      printf "%s is intended to be anonymously pullable; Docker Hub sign-in is not required.\n" "${image_reference:-$IMAGE_REPOSITORY:$IMAGE_TAG}"
      printf "Check internet access and Docker status. If the denial continues, it is likely a Studio Seventeen publication or tag configuration failure.\n"
      printf "Contact Studio Seventeen with version %s and the pull error.\n" "$IMAGE_TAG"
      printf "Type retry to pull again or quit to exit setup: "
      IFS= read -r response
      case "$response" in
        retry|"")
          ;;
        quit)
          return 1
          ;;
        *)
          printf "Unknown option.\n"
          ;;
      esac
    fi
  done
}

step_launch() {
  printf "\nStep 6 of 6 - Launch simulator\n"
  export SIM_HOST_BIND
  export GENSIM_IMAGE_TAG="$IMAGE_TAG"
  SIM_HOST_BIND="$(host_bind)"

  ensure_generator_image || return 1
  compose up -d generator
  if wait_for_url "Generator Fleet web service" "http://localhost:5001/api/live"; then
    configure_fleet_from_launcher || return 1
    if [ -n "$FLEET_STARTUP_PAYLOAD" ]; then
      wait_for_url "Generator Fleet readiness" "http://localhost:5001/api/ready" || return 1
    fi
    show_access_info
  else
    printf "\nThe container started, but the dashboard did not become ready.\n"
    printf "Check the container logs, then run setup again after resolving the issue:\n"
    printf "  docker compose --file %s logs --tail=100 generator\n" "$COMPOSE_FILE"
    return 1
  fi
}

guided_setup() {
  print_header
  check_launcher_prerequisites
  verify_release_integrity
  step_docker
  step_license
  step_security
  step_network
  step_fleet_config
  if ! step_launch; then
    printf "\nSetup did not complete. Return to the menu and try again after the issue is resolved.\n"
  fi
}

stop_simulator() {
  ensure_docker_installed
  ensure_docker_running
  ensure_compose_available
  export SIM_HOST_BIND
  export GENSIM_IMAGE_TAG="$IMAGE_TAG"
  SIM_HOST_BIND="$(host_bind)"
  compose stop generator
}

show_logs() {
  ensure_docker_installed
  ensure_docker_running
  ensure_compose_available
  compose logs -f --tail=100 generator
}

main_menu() {
  while true; do
    print_header
    printf "This setup starts Generator Fleet Simulator Community Edition only.\n"
    printf "The setup will walk through Docker, license, security, network, fleet configuration, and launch.\n\n"
    printf "1) Start guided setup\n"
    printf "2) Exit\n\n"
    printf "Choose an option: "
    IFS= read -r choice

    case "$choice" in
      1) guided_setup; pause ;;
      2) exit 0 ;;
      *) printf "Unknown option.\n"; pause ;;
    esac
  done
}

main_menu
