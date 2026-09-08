#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FASTAPI_DIR="$SCRIPT_DIR/servers/fastapi"
NEXTJS_DIR="$SCRIPT_DIR/servers/nextjs"
RUNTIME_DIR="${PRESENTON_RUNTIME_DIR:-$SCRIPT_DIR/.runtime}"
PID_DIR="$RUNTIME_DIR/pids"
LOG_DIR="$RUNTIME_DIR/logs"

FASTAPI_HOST="${FASTAPI_HOST:-127.0.0.1}"
FASTAPI_PORT="${FASTAPI_PORT:-5001}"
NEXTJS_HOST="${NEXTJS_HOST:-127.0.0.1}"
NEXTJS_PORT="${NEXTJS_PORT:-32123}"
NEXTJS_MODE="${NEXTJS_MODE:-dev}"

PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/.venv/bin/python}"
NEXT_BIN="$NEXTJS_DIR/node_modules/next/dist/bin/next"

APP_DATA_DIRECTORY="${APP_DATA_DIRECTORY:-$SCRIPT_DIR/app_data}"
TEMP_DIRECTORY="${TEMP_DIRECTORY:-$RUNTIME_DIR/tmp}"
EXPORT_RUNTIME_DIR="${EXPORT_RUNTIME_DIR:-$SCRIPT_DIR/presentation-export}"
USER_CONFIG_PATH="${USER_CONFIG_PATH:-$APP_DATA_DIRECTORY/userConfig.json}"

LLM="${LLM:-custom}"
CUSTOM_LLM_URL="${CUSTOM_LLM_URL:-http://127.0.0.1:18081/v1}"
CUSTOM_LLM_API_KEY="${CUSTOM_LLM_API_KEY:-rail-vllm-test-key}"
CUSTOM_MODEL="${CUSTOM_MODEL:-qwen3-vl-8b-instruct}"
LLM_MAX_OUTPUT_TOKENS="${LLM_MAX_OUTPUT_TOKENS:-2048}"

NEXT_PUBLIC_FAST_API="${NEXT_PUBLIC_FAST_API:-http://127.0.0.1:$FASTAPI_PORT}"
NEXT_PUBLIC_URL="${NEXT_PUBLIC_URL:-http://127.0.0.1:$NEXTJS_PORT}"

FASTAPI_PID_FILE="$PID_DIR/fastapi.pid"
NEXTJS_PID_FILE="$PID_DIR/nextjs.pid"
FASTAPI_LOG="$LOG_DIR/fastapi.log"
NEXTJS_LOG="$LOG_DIR/nextjs.log"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() {
    printf "%b%s%b\n" "$GREEN" "$*" "$NC"
}

warn() {
    printf "%b%s%b\n" "$YELLOW" "$*" "$NC" >&2
}

error() {
    printf "%b%s%b\n" "$RED" "$*" "$NC" >&2
}

usage() {
    cat <<'EOF'
Usage: ./presenton.sh <command> [component]

Commands:
  start [all|fastapi|nextjs]    Start services (default: all)
  stop [all|fastapi|nextjs]     Stop services (default: all)
  restart [all|fastapi|nextjs]  Restart services (default: all)
  status                        Show Presenton and Qwen status
  logs [all|fastapi|nextjs]     Show the last 100 log lines
  logs -f [all|fastapi|nextjs]  Follow logs
  help                          Show this help

Common environment overrides:
  FASTAPI_PORT=5001 NEXTJS_PORT=32123 NEXTJS_MODE=dev|production
  CUSTOM_LLM_URL=http://127.0.0.1:18081/v1
  CUSTOM_LLM_API_KEY=rail-vllm-test-key
  CUSTOM_MODEL=qwen3-vl-8b-instruct

API:
  POST http://127.0.0.1:5001/api/v1/generate-file
  Docs http://127.0.0.1:5001/docs
EOF
}

ensure_runtime_dirs() {
    mkdir -p "$PID_DIR" "$LOG_DIR" "$APP_DATA_DIRECTORY" "$TEMP_DIRECTORY"
    touch "$FASTAPI_LOG" "$NEXTJS_LOG"
}

pid_matches_component() {
    local pid="$1"
    local component="$2"
    local command_line

    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"

    case "$component" in
        fastapi) [[ "$command_line" == *"uvicorn"* && "$command_line" == *"api.main:app"* ]] ;;
        nextjs) [[ "$command_line" == *"next/dist/bin/next"* ]] ;;
        *) return 1 ;;
    esac
}

component_pid() {
    local component="$1"
    local pid_file
    local pid

    case "$component" in
        fastapi) pid_file="$FASTAPI_PID_FILE" ;;
        nextjs) pid_file="$NEXTJS_PID_FILE" ;;
        *) return 1 ;;
    esac

    [[ -f "$pid_file" ]] || return 1
    pid="$(<"$pid_file")"
    pid_matches_component "$pid" "$component" || return 1
    printf '%s\n' "$pid"
}

remove_stale_pid_file() {
    local component="$1"
    local pid_file

    case "$component" in
        fastapi) pid_file="$FASTAPI_PID_FILE" ;;
        nextjs) pid_file="$NEXTJS_PID_FILE" ;;
        *) return ;;
    esac

    if [[ -f "$pid_file" ]] && ! component_pid "$component" >/dev/null; then
        rm -f "$pid_file"
    fi
}

port_in_use() {
    local port="$1"
    ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .
}

wait_for_url() {
    local name="$1"
    local url="$2"
    local pid="$3"
    local attempts="${4:-120}"
    local index

    for ((index = 0; index < attempts; index++)); do
        if curl --noproxy '*' --silent --fail --max-time 2 --output /dev/null "$url"; then
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            error "$name exited during startup. Check its log file."
            return 1
        fi
        sleep 0.5
    done

    error "$name did not become ready at $url"
    return 1
}

find_chrome() {
    if [[ -n "${PUPPETEER_EXECUTABLE_PATH:-}" ]]; then
        printf '%s\n' "$PUPPETEER_EXECUTABLE_PATH"
        return
    fi

    local candidate
    for candidate in /usr/bin/google-chrome /usr/bin/chromium /usr/bin/chromium-browser; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    return 1
}

check_qwen() {
    local models_url="${CUSTOM_LLM_URL%/}/models"
    local response

    if ! response="$(curl --noproxy '*' --silent --fail --max-time 5 \
        -H "Authorization: Bearer $CUSTOM_LLM_API_KEY" "$models_url")"; then
        error "Qwen is unavailable: $models_url"
        return 1
    fi

    if [[ "$response" != *"$CUSTOM_MODEL"* ]]; then
        error "Qwen is reachable, but model '$CUSTOM_MODEL' was not returned by /v1/models."
        return 1
    fi

    return 0
}

preflight_fastapi() {
    if [[ ! -x "$PYTHON_BIN" ]]; then
        error "Python environment not found: $PYTHON_BIN"
        error "Install the project dependencies before starting the service."
        return 1
    fi
    if ! command -v setsid >/dev/null 2>&1; then
        error "setsid is required to run Presenton in the background."
        return 1
    fi
    check_qwen
}

preflight_nextjs() {
    if ! command -v node >/dev/null 2>&1; then
        error "node is not installed or not available in PATH."
        return 1
    fi
    if ! command -v setsid >/dev/null 2>&1; then
        error "setsid is required to run Presenton in the background."
        return 1
    fi
    if [[ ! -f "$NEXT_BIN" ]]; then
        error "Next.js dependencies are missing: $NEXT_BIN"
        error "Run 'npm ci' in servers/nextjs first."
        return 1
    fi
    if [[ ! -d "$EXPORT_RUNTIME_DIR" ]]; then
        error "Presentation export runtime is missing: $EXPORT_RUNTIME_DIR"
        error "Run 'npm run sync:presentation-export' in the project root first."
        return 1
    fi
    if ! find_chrome >/dev/null; then
        error "Chrome/Chromium was not found; PPT export requires a browser runtime."
        return 1
    fi
    if [[ "$NEXTJS_MODE" == "production" && ! -f "$NEXTJS_DIR/.next-build/BUILD_ID" ]]; then
        error "Next.js production build is missing. Run 'npm run build' in servers/nextjs."
        return 1
    fi
    if [[ "$NEXTJS_MODE" != "dev" && "$NEXTJS_MODE" != "production" ]]; then
        error "NEXTJS_MODE must be 'dev' or 'production'."
        return 1
    fi
}

start_fastapi() {
    local pid

    remove_stale_pid_file fastapi
    if pid="$(component_pid fastapi)"; then
        warn "FastAPI is already running (PID $pid)."
        return 0
    fi
    if port_in_use "$FASTAPI_PORT"; then
        error "Port $FASTAPI_PORT is already in use; FastAPI was not started."
        return 1
    fi
    preflight_fastapi || return 1

    printf '\n[%s] Starting FastAPI\n' "$(date '+%F %T')" >>"$FASTAPI_LOG"
    (
        cd "$FASTAPI_DIR" || exit 1
        unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
        export NO_PROXY="127.0.0.1,localhost"
        export no_proxy="$NO_PROXY"
        export APP_DATA_DIRECTORY TEMP_DIRECTORY USER_CONFIG_PATH
        export LLM CUSTOM_LLM_URL CUSTOM_LLM_API_KEY CUSTOM_MODEL LLM_MAX_OUTPUT_TOKENS
        export NEXT_PUBLIC_FAST_API NEXT_PUBLIC_URL
        export ICON_SEARCH_MODE="${ICON_SEARCH_MODE:-lexical}"
        export DISABLE_IMAGE_GENERATION="${DISABLE_IMAGE_GENERATION:-true}"
        export WEB_GROUNDING="${WEB_GROUNDING:-false}"
        export MEM0_ENABLED="${MEM0_ENABLED:-false}"
        export DISABLE_ANONYMOUS_TRACKING="${DISABLE_ANONYMOUS_TRACKING:-true}"
        export DISABLE_AUTH="${DISABLE_AUTH:-true}"
        export CAN_CHANGE_KEYS="${CAN_CHANGE_KEYS:-false}"
        export MIGRATE_DATABASE_ON_STARTUP="${MIGRATE_DATABASE_ON_STARTUP:-true}"
        exec setsid "$PYTHON_BIN" -m uvicorn api.main:app \
            --host "$FASTAPI_HOST" --port "$FASTAPI_PORT"
    ) </dev/null >>"$FASTAPI_LOG" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" >"$FASTAPI_PID_FILE"

    if ! wait_for_url "FastAPI" "$NEXT_PUBLIC_FAST_API/openapi.json" "$pid"; then
        tail -n 30 "$FASTAPI_LOG" >&2
        remove_stale_pid_file fastapi
        return 1
    fi

    info "FastAPI started (PID $pid): $NEXT_PUBLIC_FAST_API"
}

start_nextjs() {
    local pid
    local chrome_path
    local next_command

    remove_stale_pid_file nextjs
    if pid="$(component_pid nextjs)"; then
        warn "Next.js is already running (PID $pid)."
        return 0
    fi
    if port_in_use "$NEXTJS_PORT"; then
        error "Port $NEXTJS_PORT is already in use; Next.js was not started."
        return 1
    fi
    preflight_nextjs || return 1
    chrome_path="$(find_chrome)"
    next_command="dev"
    [[ "$NEXTJS_MODE" == "production" ]] && next_command="start"

    printf '\n[%s] Starting Next.js (%s)\n' "$(date '+%F %T')" "$NEXTJS_MODE" >>"$NEXTJS_LOG"
    (
        cd "$NEXTJS_DIR" || exit 1
        unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
        export NO_PROXY="127.0.0.1,localhost"
        export no_proxy="$NO_PROXY"
        export NEXT_PUBLIC_FAST_API NEXT_PUBLIC_URL APP_DATA_DIRECTORY TEMP_DIRECTORY
        export USER_CONFIG_PATH
        export EXPORT_RUNTIME_DIR
        export PUPPETEER_EXECUTABLE_PATH="$chrome_path"
        export NEXT_TELEMETRY_DISABLED=1
        exec setsid node "$NEXT_BIN" "$next_command" --hostname "$NEXTJS_HOST" --port "$NEXTJS_PORT"
    ) </dev/null >>"$NEXTJS_LOG" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" >"$NEXTJS_PID_FILE"

    if ! wait_for_url "Next.js" "$NEXT_PUBLIC_URL" "$pid"; then
        tail -n 30 "$NEXTJS_LOG" >&2
        remove_stale_pid_file nextjs
        return 1
    fi

    info "Next.js started (PID $pid): $NEXT_PUBLIC_URL"
}

stop_component() {
    local component="$1"
    local label="$2"
    local pid_file="$3"
    local pid
    local process_group
    local index

    if ! pid="$(component_pid "$component")"; then
        remove_stale_pid_file "$component"
        warn "$label is not running."
        return 0
    fi

    process_group="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [[ "$process_group" == "$pid" ]]; then
        kill -TERM -- "-$pid" 2>/dev/null || true
    else
        kill -TERM "$pid" 2>/dev/null || true
    fi
    for ((index = 0; index < 50; index++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file"
            info "$label stopped."
            return 0
        fi
        sleep 0.2
    done

    warn "$label did not stop in time; sending SIGKILL to PID $pid."
    if [[ "$process_group" == "$pid" ]]; then
        kill -KILL -- "-$pid" 2>/dev/null || true
    else
        kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
}

start_services() {
    local component="${1:-all}"
    local fastapi_was_running=false
    ensure_runtime_dirs

    case "$component" in
        all)
            if component_pid fastapi >/dev/null; then
                fastapi_was_running=true
            fi
            start_fastapi || return 1
            if ! start_nextjs; then
                if [[ "$fastapi_was_running" == false ]]; then
                    error "Next.js failed to start; stopping FastAPI started for this stack."
                    stop_component fastapi FastAPI "$FASTAPI_PID_FILE"
                else
                    error "Next.js failed to start; the pre-existing FastAPI process was left running."
                fi
                return 1
            fi
            ;;
        fastapi) start_fastapi ;;
        nextjs) start_nextjs ;;
        *) error "Unknown component: $component"; usage; return 2 ;;
    esac

    printf '\nAPI:  %s/api/v1/generate-file\nDocs: %s/docs\n' \
        "$NEXT_PUBLIC_FAST_API" "$NEXT_PUBLIC_FAST_API"
}

stop_services() {
    local component="${1:-all}"
    ensure_runtime_dirs

    case "$component" in
        all)
            stop_component nextjs Next.js "$NEXTJS_PID_FILE"
            stop_component fastapi FastAPI "$FASTAPI_PID_FILE"
            ;;
        fastapi) stop_component fastapi FastAPI "$FASTAPI_PID_FILE" ;;
        nextjs) stop_component nextjs Next.js "$NEXTJS_PID_FILE" ;;
        *) error "Unknown component: $component"; usage; return 2 ;;
    esac
}

print_component_status() {
    local component="$1"
    local label="$2"
    local url="$3"
    local pid

    remove_stale_pid_file "$component"
    if pid="$(component_pid "$component")"; then
        printf '%-10s %bRUNNING%b  PID %-8s %s\n' "$label" "$GREEN" "$NC" "$pid" "$url"
        return 0
    fi
    printf '%-10s %bSTOPPED%b\n' "$label" "$RED" "$NC"
    return 1
}

show_status() {
    local result=0
    local models_url="${CUSTOM_LLM_URL%/}/models"

    ensure_runtime_dirs
    print_component_status fastapi FastAPI "$NEXT_PUBLIC_FAST_API" || result=1
    print_component_status nextjs Next.js "$NEXT_PUBLIC_URL" || result=1
    if check_qwen >/dev/null 2>&1; then
        printf '%-10s %bREACHABLE%b %s (%s)\n' Qwen "$GREEN" "$NC" "$models_url" "$CUSTOM_MODEL"
    else
        printf '%-10s %bUNAVAILABLE%b %s\n' Qwen "$RED" "$NC" "$models_url"
        result=1
    fi
    return "$result"
}

show_logs() {
    local follow=false
    local component=all
    local tail_args=(-n 100)

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -f|--follow) follow=true ;;
            all|fastapi|nextjs) component="$1" ;;
            *) error "Unknown logs option: $1"; usage; return 2 ;;
        esac
        shift
    done

    ensure_runtime_dirs
    [[ "$follow" == true ]] && tail_args+=(-f)
    case "$component" in
        fastapi) tail "${tail_args[@]}" "$FASTAPI_LOG" ;;
        nextjs) tail "${tail_args[@]}" "$NEXTJS_LOG" ;;
        all) tail "${tail_args[@]}" "$FASTAPI_LOG" "$NEXTJS_LOG" ;;
    esac
}

main() {
    local command="${1:-help}"
    shift || true

    case "$command" in
        start) start_services "${1:-all}" ;;
        stop) stop_services "${1:-all}" ;;
        restart)
            stop_services "${1:-all}" || return 1
            start_services "${1:-all}"
            ;;
        status) show_status ;;
        logs) show_logs "$@" ;;
        help|-h|--help) usage ;;
        *) error "Unknown command: $command"; usage; return 2 ;;
    esac
}

main "$@"
