#!/usr/bin/env bash
# 编译并通过 WiFi 发布新固件：上传到服务器的 PUT /api/firmware，屏幕下次醒来看到版本号不一样就自己升级。
# 发布前先改 src/version.h 的版本号。
#   MENU_SERVER        服务器地址，如 http://192.168.1.10:8000（必填）
#   MENU_ADMIN_TOKEN   服务器设了管理 token 时填（可选）
#   PUBLISH_WITH_WIFI=1  把 MENU_WIFI_SSID/MENU_WIFI_PASS 也编进去（默认不编，见下）
# 发布出去的 firmware.bin 谁都能从 /firmware.bin 下载，所以默认不把 WiFi 密码编进去：
# 屏幕第一次连上服务器时已经把 WiFi 配置存进了 NVS，升级不会丢。
set -euo pipefail
cd "$(dirname "$0")"

: "${MENU_SERVER:?请设置 MENU_SERVER，例如 export MENU_SERVER=http://192.168.1.10:8000}"
SERVER=${MENU_SERVER%/}
V=$(sed -n 's/.*FW_VERSION "\(.*\)".*/\1/p' src/version.h)
[ -n "$V" ] || { echo "src/version.h 里没读到 FW_VERSION" >&2; exit 1; }

CORE_DIR=${PLATFORMIO_CORE_DIR:-$HOME/.platformio}
pio_cmd() {
  if command -v pio >/dev/null 2>&1; then pio "$@"
  elif [ -x "$CORE_DIR/penv/bin/pio" ]; then "$CORE_DIR/penv/bin/pio" "$@"
  elif command -v uvx >/dev/null 2>&1; then uvx platformio "$@"
  else python3 -m platformio "$@"
  fi
}

if [ "${PUBLISH_WITH_WIFI:-}" = "1" ]; then
  pio_cmd run
else
  (unset MENU_WIFI_SSID MENU_WIFI_PASS; pio_cmd run)
fi

BIN=.pio/build/epd/firmware.bin
# token 走标准输入传给 curl，不出现在命令行（ps 看得到命令行）
printf '%s' "${MENU_ADMIN_TOKEN:+Authorization: Bearer $MENU_ADMIN_TOKEN}" |
  curl -fsS -X PUT -H @- \
    -H "X-Fw-Version: $V" -H "Content-Type: application/octet-stream" \
    --data-binary @"$BIN" "$SERVER/api/firmware"
echo
echo "已发布 ${V}（$(wc -c < "$BIN" | tr -d ' ') 字节），屏幕下次醒来时升级"
