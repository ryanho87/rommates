#!/bin/bash
# Ship a ROMmates build: bump the build number, archive, upload to App Store Connect, wait for processing.
# Two TestFlight channels (App Store Connect, internal groups):
#   Nightly  gets every build automatically (Ryan, Jordan). Default: no announcement, nobody else sees it.
#   Stable   gets only promoted builds (everyone else). --stable adds this build to Stable and announces;
#            or bless a nightly later with scripts/promote.sh <build> --body "..".
# Usage: ship.sh [--stable] [--no-wait] [--patch|--minor|--major] [--body ".."]
set -euo pipefail
cd "$(dirname "$0")/.."
AUTH=(-authenticationKeyPath "$HOME/.appstoreconnect/private_keys/AuthKey_RZ3522K2CN.p8" -authenticationKeyID RZ3522K2CN -authenticationKeyIssuerID 30990242-ebdc-49b3-9961-f9d745ce67bd)
CHANNEL=dev; WAIT=1; BUMP=""; EXTRA=()
while [ $# -gt 0 ]; do case $1 in
  --stable) CHANNEL=stable;; --no-wait) WAIT=0;; --patch|--minor|--major) BUMP=${1#--};;
  --body) EXTRA+=(--body "$2"); shift;; --title) EXTRA+=(--title "$2"); shift;;
esac; shift; done
cur=$(grep -E '^[[:space:]]*CURRENT_PROJECT_VERSION:' project.yml | head -1 | grep -oE '[0-9]+')
next=$((cur + 1))
sed -i '' -E "s/^([[:space:]]*CURRENT_PROJECT_VERSION: )\"?[0-9]+\"?/\1$next/" project.yml
ver=$(grep -E '^[[:space:]]*MARKETING_VERSION:' project.yml | head -1 | grep -oE '[0-9]+(\.[0-9]+)*')
if [ -n "$BUMP" ]; then
  IFS=. read -r major minor patch <<< "$ver"; minor=${minor:-0}; patch=${patch:-0}
  case $BUMP in patch) patch=$((patch + 1));; minor) minor=$((minor + 1)); patch=0;; major) major=$((major + 1)); minor=0; patch=0;; esac
  ver="$major.$minor.$patch"
  sed -i '' -E "s/^([[:space:]]*MARKETING_VERSION: )\"?[0-9.]+\"?/\1$ver/" project.yml
fi
echo "==> ROMmates $ver ($next) [$CHANNEL]"
xcodegen generate -q
mkdir -p build
xcodebuild -project ROMmates.xcodeproj -scheme ROMmates -configuration Release -destination "generic/platform=iOS" \
  -archivePath build/ROMmates.xcarchive -allowProvisioningUpdates "${AUTH[@]}" archive > build/archive.log 2>&1 \
  && echo "==> archived" || { grep -E "error:" build/archive.log | head -5; exit 1; }
xcodebuild -exportArchive -archivePath build/ROMmates.xcarchive -exportOptionsPlist scripts/ExportOptions.plist -exportPath build/export -allowProvisioningUpdates "${AUTH[@]}" > build/upload.log 2>&1 \
  && echo "==> uploaded to App Store Connect" || { grep -E "error:" build/upload.log | head -3; exit 1; }
if [ $WAIT = 1 ]; then
  node scripts/await-build.js "$next" --channel "$CHANNEL" ${EXTRA[@]+"${EXTRA[@]}"}
else
  echo "==> not waiting; later: node scripts/await-build.js $next --channel $CHANNEL"
fi
