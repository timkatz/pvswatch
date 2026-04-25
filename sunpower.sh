#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Detect docker-compose command ──────────────────────────────────────
if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: docker compose (or docker-compose) not found."
    echo "Install Docker Desktop or docker-compose first."
    exit 1
fi

# ── Load .env ─────────────────────────────────────────────────────────
ENV_FILE="$SCRIPT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC2046
    set -a; eval $(grep -v '^\s*#' "$ENV_FILE" | grep -v '^\s*$'); set +a
fi

# ── Configuration ──────────────────────────────────────────────────────
IMAGE_NAME="sunpower-monitor"
CONTAINER_NAME="sunpower-monitor"
PORT="${DASHBOARD_PORT:-5001}"
PVS_IP="${PVS_IP:-}"
PVS_USER="${PVS_USER:-ssm_owner}"
PVS_PASS="${PVS_PASS:-}"

# ── Functions ─────────────────────────────────────────────────────────
usage() {
    cat <<EOF
SunPower Web Monitor — Docker manager

Usage: $(basename "$0") <command>

Commands:
  build     Build (or rebuild) the Docker image
  start     Start the container (builds first if needed)
  stop      Stop and remove the container
  restart   Stop and start the container
  status    Show container status and recent logs
  logs      Follow container logs
  url       Print the dashboard URL
  open      Open the dashboard in your browser
  update    git pull origin and rebuild the container
  help      Show this help message

All config is read from .env. Key variables:

  PVS_IP            PVS gateway IP
  PVS_USER           PVS auth username (default: ssm_owner)
  PVS_PASS           PVS auth password (last 5 chars of internal serial)
  PVS_SERIAL         Full internal serial (for reference)
  DASHBOARD_PORT     Host port (default: 5001)
  TIMEOUT_SECS       PVS request timeout (default: 60)
  LOG_LEVEL          Python log level (default: INFO)

EOF
}

is_running() {
    docker ps -q -f name="$CONTAINER_NAME" | grep -q .
}

needs_build() {
    docker images -q "$IMAGE_NAME" | grep -q .
}

build_image() {
    echo "🔨 Building $IMAGE_NAME..."
    $DC build
    echo "✅ Image built."
}

start_container() {
    if is_running; then
        echo "⚡ Container $CONTAINER_NAME is already running."
        return 0
    fi

    if ! needs_build; then
        build_image
    fi

    echo "🚀 Starting $CONTAINER_NAME on port $PORT..."
    $DC up -d
    echo ""
    echo "✅ Dashboard available at: http://localhost:$PORT/"
}

stop_container() {
    if ! is_running; then
        echo "Container $CONTAINER_NAME is not running."
        return 0
    fi
    echo "🛑 Stopping $CONTAINER_NAME..."
    $DC down
    echo "✅ Stopped."
}

show_status() {
    if is_running; then
        echo "✅ Container $CONTAINER_NAME is running."
        echo ""
        $DC ps
        echo ""
        echo "Recent logs:"
        $DC logs --tail=20
    else
        echo "❌ Container $CONTAINER_NAME is not running."
    fi
}

show_url() {
    echo "http://localhost:$PORT/"
}

open_dashboard() {
    local url="http://localhost:$PORT/"
    echo "Opening: $url"
    open "$url" 2>/dev/null || xdg-open "$url" 2>/dev/null || echo "Open this URL in your browser: $url"
}

update_project() {
    if [[ ! -d "$SCRIPT_DIR/.git" ]]; then
        echo "❌ Not a git checkout — 'update' only works when this repo was cloned with git."
        echo "   Pull the latest source manually, then run: $(basename "$0") restart"
        exit 1
    fi

    echo "📥 Pulling latest changes from origin..."
    if ! git -C "$SCRIPT_DIR" pull --ff-only; then
        echo "❌ git pull failed (uncommitted local changes? non-fast-forward?)."
        echo "   Resolve the conflict, then run: $(basename "$0") restart"
        exit 1
    fi

    echo ""
    echo "🔨 Rebuilding container..."
    build_image

    if is_running; then
        echo "🔄 Restarting container..."
        $DC down && $DC up -d
    fi

    echo "✅ Update complete."
}

# ── Main ──────────────────────────────────────────────────────────────
case "${1:-help}" in
    build)   build_image ;;
    start)   start_container ;;
    stop)    stop_container ;;
    restart) stop_container; start_container ;;
    status)  show_status ;;
    logs)    $DC logs -f ;;
    url)     show_url ;;
    open)    open_dashboard ;;
    update)  update_project ;;
    help|-h|--help) usage ;;
    *)       echo "Unknown command: $1"; echo; usage; exit 1 ;;
esac