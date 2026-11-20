#!/bin/bash
# APPC Tablet Dashboard - double-click to start (Mac). Close this window or press Ctrl+C to stop it.
cd "$(dirname "$0")" && exec python3 appc_dashboard.py "$@"
