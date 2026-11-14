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

# Upload with the Xcode account; if that fails and an App Store Connect API
# key is configured, retry with the key. The account sign-in can lapse
# ("Failed to Use Accounts"). Key env file: ASC_KEY_PATH, ASC_KEY_ID,
# ASC_ISSUER_ID (kept outside the repo; ASC_ENV overrides the path).
auth=()
asc_env=${ASC_ENV:-$HOME/.appstoreconnect/arc.env}
if [ -f "$asc_env" ]; then
  # shellcheck disable=SC1090
  . "$asc_env"
  auth=(-authenticationKeyPath "${ASC_KEY_PATH/#\~/$HOME}" -authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER_ID")
fi

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
  export_() { xcodebuild -exportArchive -archivePath "$out/PvsWatch.xcarchive" -exportOptionsPlist "$out/export.plist" \
    -exportPath "$out/export" -allowProvisioningUpdates "$@" 2>&1 | tail -5; }
  if ! export_ | tee /dev/stderr | grep -q "EXPORT SUCCEEDED"; then
    [ ${#auth[@]} -gt 0 ] || exit 1
    echo "retrying $1 upload with the App Store Connect API key"
    export_ "${auth[@]}" | tee /dev/stderr | grep -q "EXPORT SUCCEEDED" || exit 1
  fi
  echo "uploaded $1 build $build"
}

case ${1:-both} in
  ios) upload PvsWatch 'generic/platform=iOS' ;;
  mac) upload PvsWatchMac 'generic/platform=macOS' ;;
  both) upload PvsWatch 'generic/platform=iOS'; upload PvsWatchMac 'generic/platform=macOS' ;;
esac
