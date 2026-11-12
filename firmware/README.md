# 墨水屏固件

合宙 ESP32-C3 核心板 + UC8179 7.5 寸黑白红三色屏（800×480，GxEPD2 的 `GxEPD2_750c_Z08`）。

每次醒来：读电量和温湿度 → 连 WiFi → `GET {服务器}/api/frame`（带 ETag）→ 画面有变化就边下载边写屏（40 行一段，黑红两层）→ 按服务器回的 `X-Sleep` 秒数深度睡眠。服务器回的 `X-Fw` 和自己的版本不一样时，从 `{服务器}/firmware.bin` 下载升级。

## 编译环境

- [PlatformIO Core](https://platformio.org/install/cli) 6.x。不想装的话，脚本会依次尝试 PATH 里的 `pio`、`~/.platformio/penv/bin/pio`、`uvx platformio`、`python3 -m platformio`
- 平台锁在 `espressif32@6.9.0`（Arduino core 2.0.17），库是 `zinggjm/GxEPD2@1.6.2`，第一次 `pio run` 会自动下载
- 刷机用 [esptool](https://github.com/espressif/esptool) 5.x，有 [uv](https://docs.astral.sh/uv/) 的话脚本直接 `uvx --from 'esptool>=5' esptool`
- 分区表 `min_spiffs.csv`：两个 1.875MB 的 OTA 分区，当前固件约 1MB

```sh
cd firmware
pio run          # 产物在 .pio/build/epd/firmware.bin
```

## 环境变量（编译时默认值）

WiFi 和服务器地址可以在编译时给默认值，也可以完全不给，刷进去以后用手机配网。

| 变量 | 说明 |
|---|---|
| `MENU_WIFI_SSID` | WiFi 名称 |
| `MENU_WIFI_PASS` | WiFi 密码 |
| `MENU_SERVER` | 服务器地址，`http://` 或 `https://` 开头，不带路径，比如 `http://192.168.1.10:8000` |
| `MENU_ADMIN_TOKEN` | 只有 `publish.sh` 用：服务器设了管理 token 时带上 |

`scripts/env_defaults.py`（PlatformIO 的 pre 脚本）把它们按字节转成 C 字符串宏，引号、空格、`$`、反斜杠、中文 SSID 都能原样编进去。没设就是空字符串。改了变量再 `pio run` 会自动重新编译。

运行时优先用 NVS 里存的配置，没有才用编译时默认值。用默认值第一次连上服务器后，会把它们存进 NVS，以后升级的固件不带默认值也能连。

> 注意：编进固件的 WiFi 密码能从 `firmware.bin` 里直接读出来。`publish.sh` 发布的固件谁都能从 `/firmware.bin` 下载，所以它默认不把 `MENU_WIFI_SSID` / `MENU_WIFI_PASS` 编进去。

## 首次有线刷机

1. 拆开前框（顶边撬卡扣，小心屏幕排线），用数据线插核心板下边缘的 Type-C。外壳侧面的 Type-C 只能充电
2. **备份原厂固件和 bootloader**（只需要做一次）：

   ```sh
   ./flash.sh --backup
   ```

   脚本等串口出现时按一下屏幕正面的键叫醒板子。它会把整片 4MB 读到 `backup/orig-full-<时间>.bin`，再截出前 32KB 存成 `bootloader-orig.bin`。也可以只备份 bootloader：

   ```sh
   uvx --from 'esptool>=5' esptool --chip esp32c3 -p /dev/cu.usbmodemXXXX read-flash 0x0 0x8000 bootloader-orig.bin
   ```

   为什么要原厂 bootloader：这块板的 Flash 只能跑 DIO，PlatformIO 编出来的 bootloader 在上面会无限重启，所以 0x0 一直用原厂的，只换分区表和应用。原厂文件是厂家的二进制，`.gitignore` 已经排除，不要提交。

3. 刷入：

   ```sh
   MENU_SERVER=http://192.168.1.10:8000 ./flash.sh   # WiFi 不写也行，等会儿配网
   ```

   串口自动找 macOS 的 `/dev/cu.usbmodem*` 和 Linux 的 `/dev/ttyACM*`，也可以用 `PORT=/dev/ttyACM0 ./flash.sh` 指定。Linux 上当前用户要在 `dialout`（或 `uucp`）组里。

想恢复原厂固件：`esptool --chip esp32c3 -p <串口> write-flash 0x0 backup/orig-full-<时间>.bin`。

## 配网

满足下面任一条件就进配网模式：

- NVS 和编译时默认值里都没有 WiFi 名称或服务器地址（不管怎么醒的）
- **按键复位或上电**时 WiFi 连不上
- **按键复位或上电**时服务器连不上或回 404（多半是地址填错了；服务器报 5xx 不进配网）

定时醒来时 WiFi 或服务器连不上，还是照旧：连续两次就在右下角画红字「未联网」，过 30 分钟（失败 4 次以上改成 1 小时）再试，不进配网，免得半夜开热点耗电。

配网流程：

1. 屏幕整屏显示英文提示和热点名 `Menu-Setup-XXXX`（XXXX 是 MAC 后 4 位）
2. 手机连这个热点（开放热点，没有密码），一般会自动弹出配网页；没弹就打开 `http://192.168.4.1`
3. 填 WiFi 名称（下拉里有扫到的网络）、密码、服务器地址，点「保存并重启」。同一个 WiFi 密码留空表示不改
4. 屏幕重启后按新配置联网，整屏重画菜单

网页 5 分钟没有请求就关热点，深度睡眠 1 小时。配网期间一直在喂看门狗。想改配置时：按正面的键前先关掉路由器或者让服务器暂时连不上，或者有线刷机前擦掉 NVS：`esptool --chip esp32c3 -p <串口> erase-region 0x9000 0x5000`。

## 之后用 WiFi 升级

1. 改 `src/version.h` 的 `FW_VERSION`
2. 发布：

   ```sh
   MENU_SERVER=http://192.168.1.10:8000 MENU_ADMIN_TOKEN=xxx ./publish.sh
   ```

   脚本编译后用 `curl` 把 `firmware.bin` 以 `PUT {服务器}/api/firmware` 上传，header 带 `X-Fw-Version: <版本号>`，设了 `MENU_ADMIN_TOKEN` 就再带 `Authorization: Bearer <token>`（token 通过标准输入交给 curl，不出现在进程命令行里）。默认不把 WiFi 默认值编进去，确实需要就加 `PUBLISH_WITH_WIFI=1`
3. 屏幕下次醒来看到 `X-Fw` 不一样就下载升级。急的话按一下正面的键马上叫醒

http 和 https 的服务器都行：`http://` 用普通连接，`https://` 用 TLS 但**不校验证书**（`setInsecure`，按局域网自用设计）。服务器放公网的话自己掂量。

## 回滚

升级成功后新固件标记为「待确认」。之后每次启动记一次，只要有一次联系上服务器（`/api/frame` 回 200 或 304）就确认可用；**连续 3 次启动都没联系上**，第 4 次启动时切回另一个 OTA 分区里的旧固件，并把这个版本号记成坏版本，以后服务器再推这个版本也不装。看门狗（150 秒）卡死重启也计入次数。

## 引脚

| 功能 | GPIO |
|---|---|
| 屏 BUSY | 10 |
| 屏 RST | 8 |
| 屏 DC | 2 |
| 屏 CS | 7 |
| 屏 SCK | 4 |
| 屏 MOSI | 6 |
| SHT4x 温湿度 SDA / SCL | 6 / 4（和屏幕共用，地址 0x44） |
| 电池电压 | 0（分压，读数 ×2） |

温湿度和屏幕共用两根线：先用 I2C 读完传感器再切成 SPI 写屏。前面的键接 EN（复位），背面的键接 BOOT（按住背面再按前面进下载模式，平时用不上）。

## 常见问题

- **Flash 必须 DIO**：`platformio.ini` 里的 `board_build.flash_mode = dio` 不能删。这块 Flash 跑 QIO 读出来是乱码，表现是自编 bootloader 每 25ms 重启、OTA 校验报 `invalid magic byte` / `Flash Read Failed`
- **电脑认不到串口**：深度睡眠时 USB 不枚举。先运行脚本，等它提示时按一下正面的键，串口一出现脚本就会抓住它
- **串口日志里有 `Busy Timeout`**：GxEPD2 等屏幕 BUSY 引脚的超时提示，三色屏全刷时间长，画面正常就可以忽略
- **温湿度读不到**：传感器读失败时服务器会收到 `diag` 诊断串（每一步的 I2C 结果），用来排查飞线接触问题
