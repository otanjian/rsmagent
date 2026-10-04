#!/bin/sh
set -eu
# Volumes are initialized by the operator with uid/gid 10001. The application
# never starts as root or recursively changes ownership of mounted user files.
mkdir -p "${COW_DATA_DIR:-/home/agent/.cow}" /home/agent/cow
if [ ! -w "${COW_DATA_DIR:-/home/agent/.cow}" ] || [ ! -w /home/agent/cow ]; then
    echo 'Instance volumes must be writable by uid/gid 10001.' >&2
    exit 1
fi
if [ -n "${CHATGPT_ON_WECHAT_EXEC:-}" ]; then
    # Retain an explicit operator-supplied legacy entry command.
    exec /bin/sh -c "$CHATGPT_ON_WECHAT_EXEC"
fi
exec "$@"
