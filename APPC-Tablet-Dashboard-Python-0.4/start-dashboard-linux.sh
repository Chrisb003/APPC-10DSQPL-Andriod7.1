#!/bin/sh
# APPC Tablet Dashboard - start it (Linux / Raspberry Pi). Press Ctrl+C to stop it.
cd "$(dirname "$0")" && exec python3 appc_dashboard.py "$@"
