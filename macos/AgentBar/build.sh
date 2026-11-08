#!/bin/zsh
# Build AgentBar.app (ad-hoc signed) into ./build and optionally install to ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
APP=build/AgentBar.app
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
swiftc -parse-as-library -swift-version 5 -O -target arm64-apple-macos14.0 -o "$APP/Contents/MacOS/AgentBar" main.swift
codesign --force --sign - "$APP"
codesign --verify "$APP" && echo "built $APP"
if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications
  pkill -x AgentBar 2>/dev/null || true
  rm -rf ~/Applications/AgentBar.app && cp -R "$APP" ~/Applications/
  open ~/Applications/AgentBar.app && echo "installed + launched ~/Applications/AgentBar.app"
fi
