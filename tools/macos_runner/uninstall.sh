#!/bin/bash
set -u
LABEL=no.djayuffe.soapy-power-runner
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null
rm -f "$PLIST"
rm -rf "$HOME/Library/Application Support/soapy-power-runner"
echo "avinstallert $LABEL"
