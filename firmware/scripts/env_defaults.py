# PlatformIO pre 脚本：把环境变量 MENU_WIFI_SSID / MENU_WIFI_PASS / MENU_SERVER 变成编译时默认值（C 字符串宏）。
# 没设就是空字符串。每个字节都写成三位八进制转义（\ooo），这样引号、空格、$、反斜杠、中文都原样进固件，
# 命令行上只剩数字、反斜杠和引号，不会被 shell 或 SCons 拆开。
import os

Import("env")  # noqa: F821  （SCons 注入）

for name in ("MENU_WIFI_SSID", "MENU_WIFI_PASS", "MENU_SERVER"):
    raw = os.environ.get(name, "")
    if name == "MENU_SERVER":
        raw = raw.strip().rstrip("/")
    body = "".join("\\\\%03o" % b for b in raw.encode("utf-8"))
    # 命令行上是 -DNAME=\"\\101\\102\"，经过 sh 之后编译器看到 -DNAME="\101\102"
    env.Append(CPPDEFINES=[(name, '\\"' + body + '\\"')])
    print("%s: %s" % (name, ("已设置（%d 字节）" % len(raw.encode("utf-8"))) if raw else "未设置"))
