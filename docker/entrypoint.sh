#!/bin/sh
set -e
if [ ! -f /data/config.yaml ]; then
    cp /app/config.yaml /data/config.yaml
fi
exec python /app/server.py "$@"
