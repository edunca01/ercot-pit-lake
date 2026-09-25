#!/usr/bin/env bash
# Runs once when the container is created.
set -euo pipefail

# The workspace is bind-mounted with the host's owner; without this git (and so pre-commit)
# refuses to run in it ("dubious ownership").
git config --global --add safe.directory "$PWD"

make setup

echo "ready: make check"
