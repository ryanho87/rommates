#!/bin/bash
# Promote a nightly to Stable: add the already-processed build to the Stable TestFlight group and announce it.
# Nothing is rebuilt. Usage: promote.sh <build> --body "release notes" [--title ".."]
set -euo pipefail
cd "$(dirname "$0")/.."
build="${1:?usage: promote.sh <build> --body \"..\"}"; shift
node scripts/await-build.js "$build" --channel stable --timeout-min 5 "$@"
