#!/bin/bash
# Build the Mac app (Release) and install it to /Applications, replacing
# any older copy, then open it.   scripts/install-mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export DEVELOPER_DIR=${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}
xcodegen generate -q
xcodebuild -project PvsWatch.xcodeproj -scheme PvsWatchMac -configuration Release \
  -destination platform=macOS -derivedDataPath build -allowProvisioningUpdates build | tail -1
pkill -x "PVS Watch" || true
rm -rf "/Applications/PVS Watch.app"
cp -R "build/Build/Products/Release/PVS Watch.app" /Applications/
open "/Applications/PVS Watch.app"
echo "installed /Applications/PVS Watch.app"
