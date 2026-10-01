#!/usr/bin/env bash
# Compile the Console to a native binary. That is what curl|bash installs.
exec "$(cd "$(dirname "$0")" && pwd)/compile-console.sh" "$@"
