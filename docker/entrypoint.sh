#!/usr/bin/env bash
set -euo pipefail

MEGA_LOG=${MEGA_LOG_PATH:-/tmp/mega-cmd-server.log}
MEGA_READY_RETRIES=${MEGA_READY_RETRIES:-30}
MEGA_READY_DELAY=${MEGA_READY_DELAY:-1}

mega-cmd-server >"${MEGA_LOG}" 2>&1 &

for ((i=1; i<=MEGA_READY_RETRIES; i++)); do
  if mega-version >/dev/null 2>&1; then
    break
  fi

  if [[ "${i}" -eq "${MEGA_READY_RETRIES}" ]]; then
    echo "MEGAcmd server did not become ready in time." >&2
    exit 1
  fi

  sleep "${MEGA_READY_DELAY}"
done

if [[ -n "${MEGA_EMAIL:-}" && -n "${MEGA_PASSWORD:-}" ]]; then
  if ! mega-whoami >/dev/null 2>&1; then
    if ! mega-login <<EOF
${MEGA_EMAIL}
${MEGA_PASSWORD}
EOF
    then
      echo "MEGA login failed using stdin credentials." >&2
      exit 1
    fi
  fi
fi

if ! mega-whoami >/dev/null 2>&1; then
  echo "MEGA authentication missing. Set MEGA_EMAIL and MEGA_PASSWORD or mount an existing MEGAcmd session." >&2
  exit 1
fi

exec "$@"
