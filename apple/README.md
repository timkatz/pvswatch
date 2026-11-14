# PVS Watch for iPhone and Mac

Native SwiftUI apps for pvswatch: **PvsWatch** (iPhone, tuned for the iPhone Duo foldable) and **PvsWatchMac** (macOS). They read only the server's `/api/v1` ([docs/API.md](../docs/API.md)), so every number matches the web dashboard. Shared code lives in `Shared/`; `iOS/` and `Mac/` hold what differs.

- **Server:** set in Settings, e.g. `https://pvswatch.your-tailnet.ts.net`. pvswatch has no login, so keep it on a private network such as Tailscale.
- **Screens:** Now (status, power tiles, flow diagram, battery and mode), History (Swift Charts with outage bands), Money (time of use, net cost and savings), Outages, Panels, System, Settings (server settings and battery mode, shared with the web).
- **iPhone postures:**
  - folded: tab bar
  - open flat: sidebar
  - book: Now on the left leaf, any section on the right
  - laptop: the flow on top, battery controls below

  Detection is the same as arc's, using `onHingeChange` plus `reservedRegions(kind: .division)`.
- **Mac:** sidebar window, Settings window (⌘,), ⌘R refreshes. Sandboxed (`network.client` only).

## Build

Requires Xcode 27 and [XcodeGen](https://github.com/yonaskolb/XcodeGen).

```bash
cp Example.xcconfig Local.xcconfig   # your team id + bundle prefix (gitignored)
xcodegen generate
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer

# iPhone simulator, pointed at a server (or the local mock rig on :5099)
xcodebuild -project PvsWatch.xcodeproj -scheme PvsWatch -destination 'platform=iOS Simulator,name=iPhone Duo' -derivedDataPath build build
xcrun simctl install booted build/Build/Products/Debug-iphonesimulator/PvsWatch.app
xcrun simctl launch booted <bundle id> -server http://localhost:5099 -section now

# Mac
xcodebuild -project PvsWatch.xcodeproj -scheme PvsWatchMac -destination platform=macOS -derivedDataPath build build
```

`-server` and `-section` are development launch arguments.

## TestFlight

`scripts/testflight.sh [ios|mac|both]` archives, signs and uploads with the Apple account signed into Xcode. The build number is the git commit count. The App Store Connect app record has to exist first, with the bundle id `<prefix>.pvswatch` from `Local.xcconfig`.
