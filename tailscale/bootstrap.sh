#!/bin/sh
set -eu

/usr/local/bin/containerboot &
containerboot_pid=$!

cleanup() {
    kill "$containerboot_pid" 2>/dev/null || true
}

trap cleanup INT TERM

until tailscale status --json >/dev/null 2>&1; do
    sleep 1
done

until tailscale ip -4 >/dev/null 2>&1; do
    sleep 1
done

tailscale funnel --yes --bg "http://127.0.0.1:${FUNNEL_TARGET_PORT:-19191}"

wait "$containerboot_pid"
