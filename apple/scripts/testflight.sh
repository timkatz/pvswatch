#!/bin/bash
# Archive the app and upload it to TestFlight with the Apple account signed
# into Xcode. Build number = git commit count, so every upload is higher than
# the last.  scripts/testflight.sh [ios|mac|both]   (default both)
set -euo pipefail
cd "$(dirname "$0")/.."
export DEVELOPER_DIR=${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}
team=$(sed -n 's/^DEVELOPMENT_TEAM *= *//p' Local.xcconfig 2>/dev/null)
[ -n "$team" ] || { echo "Set DEVELOPMENT_TEAM in Local.xcconfig (copy Example.xcconfig)"; exit 1; }
build=$(git rev-list --count HEAD)
xcodegen generate -q

upload() { # scheme destination
  out=build/testflight/$1
  rm -rf "$out" && mkdir -p "$out"
  xcodebuild -project PvsWatch.xcodeproj -scheme "$1" -configuration Release \
    -destination "$2" -archivePath "$out/PvsWatch.xcarchive" -allowProvisioningUpdates \
    CURRENT_PROJECT_VERSION="$build" archive | tail -3
  cat > "$out/export.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>upload</string>
  <key>teamID</key><string>$team</string>
  <key>signingStyle</key><string>automatic</string>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST
  xcodebuild -exportArchive -archivePath "$out/PvsWatch.xcarchive" -exportOptionsPlist "$out/export.plist" \
    -exportPath "$out/export" -allowProvisioningUpdates 2>&1 | tail -5 | tee /dev/stderr | grep -q "EXPORT SUCCEEDED"
  echo "uploaded $1 build $build"
}

case ${1:-both} in
  ios) upload PvsWatch 'generic/platform=iOS' ;;
  mac) upload PvsWatchMac 'generic/platform=macOS' ;;
  both) upload PvsWatch 'generic/platform=iOS'; upload PvsWatchMac 'generic/platform=macOS' ;;
esac
