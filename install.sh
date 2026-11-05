#!/bin/zsh
# 安装/更新爆款雷达的两个后台任务：本机界面服务 + 每 30 分钟一次的自动运行。卸载：./install.sh remove
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/m3air/.local/share/uv/python/cpython-3.12.15-macos-aarch64-none/bin/python3.12"
DATA_DIR="$HOME/Library/Application Support/BaokuanRadar"
LOGS="$DATA_DIR/logs"
AGENTS="$HOME/Library/LaunchAgents"
if [[ "${1:-}" == remove ]]; then
  for name in server tick; do
    launchctl bootout gui/$(id -u)/com.azu.baokuan.$name 2>/dev/null || true
    rm -f "$AGENTS/com.azu.baokuan.$name.plist"
  done
  echo "已卸载后台任务，已有数据保留"; exit 0
fi
if [[ ! -x "$PY" ]]; then echo "找不到真实 Python 解释器：$PY" >&2; exit 1; fi
mkdir -p "$LOGS" "$AGENTS"
for name in server tick; do launchctl bootout gui/$(id -u)/com.azu.baokuan.$name 2>/dev/null || true; done
cat > "$AGENTS/com.azu.baokuan.server.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.azu.baokuan.server</string>
<key>ProgramArguments</key><array><string>$PY</string><string>$DIR/server.py</string></array>
<key>WorkingDirectory</key><string>$DATA_DIR</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$LOGS/server.log</string><key>StandardErrorPath</key><string>$LOGS/server.log</string>
</dict></plist>
PLIST
cat > "$AGENTS/com.azu.baokuan.tick.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.azu.baokuan.tick</string>
<key>ProgramArguments</key><array><string>$PY</string><string>$DIR/radar.py</string><string>tick</string></array>
<key>WorkingDirectory</key><string>$DATA_DIR</string>
<key>StartInterval</key><integer>1800</integer>
<key>StandardOutPath</key><string>$LOGS/tick.log</string><key>StandardErrorPath</key><string>$LOGS/tick.log</string>
</dict></plist>
PLIST
for name in server tick; do launchctl bootstrap gui/$(id -u) "$AGENTS/com.azu.baokuan.$name.plist"; done
echo "已安装：界面 http://127.0.0.1:8777 ，每 30 分钟自动运行一次"
