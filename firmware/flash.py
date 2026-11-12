# /// script
# requires-python = ">=3.10"
# dependencies = ["esptool>=5", "platformio"]
# ///
"""USB 有线刷机（第一次或救砖用），macOS / Linux / Windows 通用。装了 uv 就行，依赖自动装：

  uv run flash.py --backup     先把板子上的原厂固件整片备份到 backup/，并截出 bootloader-orig.bin
  uv run flash.py --ssid 家里WiFi --password 密码 --server http://192.168.1.10:8000
                               编译并刷入（需要 bootloader-orig.bin）。三个参数都能不给，用环境变量
                               MENU_WIFI_SSID / MENU_WIFI_PASS / MENU_SERVER，或者刷完用手机配网

串口自动找乐鑫原生 USB（VID 303A），也可以 --port COM5 或 PORT=/dev/ttyACM0 指定。
板子深度睡眠时 USB 不枚举，脚本等串口时按一下屏幕边框上那个白色长条按键（复位）叫醒，串口一出现就会被抓住进下载模式。
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from serial.tools import list_ports

HERE = Path(__file__).resolve().parent
BOOTLOADER = HERE / "bootloader-orig.bin"
CORE_DIR = Path(os.environ.get("PLATFORMIO_CORE_DIR") or Path.home() / ".platformio")
BOOT_APP0 = CORE_DIR / "packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin"
BUILD = HERE / ".pio/build/epd"
ESPRESSIF_VID = 0x303A

MISSING_BOOTLOADER = f"""缺 {BOOTLOADER.name}（板子出厂时的 bootloader）。

为什么要它：这块板 PlatformIO 编出来的 bootloader 会无限重启（Flash 只能跑 DIO），
所以刷机时 0x0 用原厂的 bootloader，只换分区表和应用。原厂文件是厂家的二进制，不放在仓库里。

第一次刷之前先备份（顺带整片备份原厂固件，方便以后恢复）：
  uv run flash.py --backup"""


def run(*cmd):
    r = subprocess.run([sys.executable, "-m", *cmd], cwd=HERE)
    if r.returncode:
        sys.exit(f"{cmd[0]} 失败（退出码 {r.returncode}），看上面的输出")


def wait_port(port):
    if port:
        return port
    print("等串口出现（按一下屏幕边框上那个白色长条按键叫醒板子）...", flush=True)
    for _ in range(1800):
        for p in list_ports.comports():
            if p.vid == ESPRESSIF_VID:
                print(f"串口：{p.device}", flush=True)
                return p.device
        time.sleep(0.1)
    sys.exit("3 分钟没等到串口。确认数据线插的是核心板自己的 Type-C（外壳侧面那个只能充电）、线能传数据；"
             "Linux 上当前用户要在 dialout/uucp 组里。也可以用 --port 指定")


def esptool(port, *args):
    run("esptool", "--chip", "esp32c3", "-p", port, "--before", "default-reset", "--after", "hard-reset", *args)


def backup(port):
    (HERE / "backup").mkdir(exist_ok=True)
    full = HERE / "backup" / f"orig-full-{time.strftime('%Y%m%d-%H%M%S')}.bin"
    # 整片 4MB：原厂固件 + bootloader + NVS，想恢复原厂时 write-flash 0x0 回去即可
    esptool(wait_port(port), "read-flash", "0x0", "0x400000", str(full))
    if BOOTLOADER.exists():
        print(f"{BOOTLOADER.name} 已存在，没有覆盖")
    else:
        # ESP32-C3 的 bootloader 在 0x0，分区表从 0x8000 开始，截前 32KB 就是原厂 bootloader
        BOOTLOADER.write_bytes(full.read_bytes()[:0x8000])
        print(f"已截出 {BOOTLOADER.name}")
    print(f"整片备份：{full}（自己留好，别提交到仓库）")


def flash(port):
    if not BOOTLOADER.exists():
        sys.exit(MISSING_BOOTLOADER)
    run("platformio", "run")
    if not BOOT_APP0.exists():
        sys.exit(f"找不到 {BOOT_APP0}，先确认 platformio run 装好了 espressif32 平台")
    esptool(wait_port(port), "write-flash", "--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", "4MB",
            "0x0", str(BOOTLOADER), "0x8000", str(BUILD / "partitions.bin"),
            "0xe000", str(BOOT_APP0), "0x10000", str(BUILD / "firmware.bin"))
    print("刷好了，屏幕会自己重启联网")


def main():
    ap = argparse.ArgumentParser(description="USB 有线刷机", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--backup", action="store_true", help="只备份原厂固件，不刷")
    ap.add_argument("--port", default=os.environ.get("PORT"), help="串口，比如 COM5、/dev/ttyACM0")
    ap.add_argument("--ssid", help="WiFi 名称（只支持 2.4G）")
    ap.add_argument("--password", help="WiFi 密码")
    ap.add_argument("--server", help="服务器地址，比如 http://192.168.1.10:8000")
    a = ap.parse_args()
    # 交给 scripts/env_defaults.py 编进固件
    for name, val in (("MENU_WIFI_SSID", a.ssid), ("MENU_WIFI_PASS", a.password), ("MENU_SERVER", a.server)):
        if val is not None:
            os.environ[name] = val
    if a.backup:
        backup(a.port)
    else:
        flash(a.port)


if __name__ == "__main__":
    main()
