#!/usr/bin/env bash
# Keep WSL attached and reconnect both private inference forwards after SSH loss.
set -u
exec 9>/tmp/hackutd-inference-tunnels.lock
flock -n 9 || { printf 'Inference tunnel supervisor is already running.\n' >&2; exit 1; }

child=''
cleanup() {
    trap - EXIT INT TERM
    if [[ -n "$child" ]]; then
        kill "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    exit
}
trap cleanup EXIT INT TERM

while true; do
    ssh -N -T -o BatchMode=yes -o ConnectTimeout=10 \
        -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
        -L 127.0.0.1:8000:127.0.0.1:8000 \
        -L 127.0.0.1:8003:127.0.0.1:8002 \
        "${1:-secure-amethyst-ferret}" 9>&- &
    child=$!
    wait "$child" || true
    child=''
    printf 'Inference connection ended; reconnecting in 3 seconds.\n' >&2
    sleep 3
done
