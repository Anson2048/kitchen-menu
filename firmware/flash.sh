#!/usr/bin/env bash
# USB 有线刷机（第一次或救砖用）。用法：
#   ./flash.sh            编译并刷入（需要 bootloader-orig.bin）
#   ./flash.sh --backup   先把板子上的原厂固件整片备份到 backup/，并截出 bootloader-orig.bin
# 串口默认自动找（macOS /dev/cu.usbmodem*，Linux /dev/ttyACM*），也可以 PORT=/dev/xxx ./flash.sh 指定。
# 板子深度睡眠时 USB 不枚举，脚本等串口时按一下屏幕正面的键（复位）叫醒，串口一出现就会被抓住进下载模式。
set -euo pipefail
cd "$(dirname "$0")"

BOOTLOADER=bootloader-orig.bin
CORE_DIR=${PLATFORMIO_CORE_DIR:-$HOME/.platformio}
BOOT_APP0=$CORE_DIR/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin
B=.pio/build/epd

# pio：优先用 PATH 里的，没有就用 PlatformIO 自带虚拟环境、uvx、python -m 兜底
pio_cmd() {
  if command -v pio >/dev/null 2>&1; then pio "$@"
  elif [ -x "$CORE_DIR/penv/bin/pio" ]; then "$CORE_DIR/penv/bin/pio" "$@"
  elif command -v uvx >/dev/null 2>&1; then uvx platformio "$@"
  else python3 -m platformio "$@"
  fi
}

# esptool 要 5.x（子命令和参数用连字符写法，如 write-flash、--flash-mode）
esptool_cmd() {
  if command -v uvx >/dev/null 2>&1; then uvx --from 'esptool>=5' esptool "$@"
  elif command -v esptool >/dev/null 2>&1; then esptool "$@"
  else python3 -m esptool "$@"
  fi
}

find_port() {
  if [ -n "${PORT:-}" ]; then echo "$PORT"; return; fi
  local p
  for p in /dev/cu.usbmodem* /dev/ttyACM*; do
    [ -e "$p" ] && { echo "$p"; return; }
  done
}

wait_port() {
  local p=""
  if [ -n "${PORT:-}" ]; then echo "$PORT"; return; fi
  echo "等串口出现（按一下屏幕正面的键叫醒板子）..." >&2
  for _ in $(seq 1 1800); do
    p=$(find_port)
    [ -n "$p" ] && break
    sleep 0.1
  done
  [ -n "$p" ] || { echo "3 分钟没等到串口。Linux 上确认当前用户在 dialout/uucp 组里，或用 PORT=... 指定" >&2; exit 1; }
  echo "串口：$p" >&2
  echo "$p"
}

if [ "${1:-}" = "--backup" ]; then
  mkdir -p backup
  P=$(wait_port)
  F=backup/orig-full-$(date +%Y%m%d-%H%M%S).bin
  # 整片 4MB：原厂固件 + bootloader + NVS，想恢复原厂时 write-flash 0x0 回去即可
  esptool_cmd --chip esp32c3 -p "$P" --before default-reset --after hard-reset read-flash 0x0 0x400000 "$F"
  if [ -e "$BOOTLOADER" ]; then
    echo "$BOOTLOADER 已存在，没有覆盖"
  else
    # ESP32-C3 的 bootloader 在 0x0，分区表从 0x8000 开始，截前 32KB 就是原厂 bootloader
    head -c 32768 "$F" > "$BOOTLOADER"
    echo "已截出 $BOOTLOADER"
  fi
  echo "整片备份：$F（自己留好，别提交到仓库）"
  exit 0
fi

if [ ! -e "$BOOTLOADER" ]; then
  cat >&2 <<MSG
缺 $BOOTLOADER（板子出厂时的 bootloader）。

为什么要它：这块板 PlatformIO 编出来的 bootloader 会无限重启（Flash 只能跑 DIO），
所以刷机时 0x0 用原厂的 bootloader，只换分区表和应用。原厂文件是厂家的二进制，不放在仓库里。

第一次刷之前先备份（推荐，顺带整片备份原厂固件，方便以后恢复）：
  ./flash.sh --backup

或者只备份 bootloader：
  uvx --from 'esptool>=5' esptool --chip esp32c3 -p <串口> read-flash 0x0 0x8000 $BOOTLOADER

（串口在 macOS 上是 /dev/cu.usbmodem*，Linux 上是 /dev/ttyACM*；板子在睡就先按正面的键叫醒）
MSG
  exit 1
fi

pio_cmd run
[ -e "$BOOT_APP0" ] || { echo "找不到 $BOOT_APP0，先确认 pio run 装好了 espressif32 平台" >&2; exit 1; }
P=$(wait_port)
esptool_cmd --chip esp32c3 -p "$P" --before default-reset --after hard-reset write-flash \
  --flash-mode dio --flash-freq 80m --flash-size 4MB \
  0x0 "$BOOTLOADER" 0x8000 $B/partitions.bin 0xe000 "$BOOT_APP0" 0x10000 $B/firmware.bin
