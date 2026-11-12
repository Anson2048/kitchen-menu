# 给 AI 助手的安装说明

用户把这个仓库交给你，让你帮他把厨房墨水屏菜单装起来。用户多半不懂编程：**命令都由你来跑**，只在必须动手（插线、按键、连 WiFi）时叫他，每次只说一件事，说清楚做完怎么告诉你。

要做完的三件事：

1. 服务端跑起来，同一个 WiFi 下的手机能打开网页
2. 屏幕刷好固件，连上服务端，显示出菜单
3. 告诉用户手机上怎么打开、先设置什么

改代码、改版面不在这份说明的范围里，那些看 [CLAUDE.md](CLAUDE.md)。

## 开始前一次问清

- **服务端放哪台机器**：屏幕每天按点醒来（默认 06:00、09:30、10:45、18:00）去服务端拿图，那时服务端必须开着。家里有一直开着的机器（NAS、软路由、迷你主机、不关机的电脑）最好；没有就装在当前这台电脑上，并告诉用户：这台电脑在那几个时间要开着，或者装好后在网页「设置 › 屏幕」里把醒来时间改到电脑开着的时候。服务端连不上时屏幕会每 30 分钟重试，右下角显示红字「未联网」，更费电
- **WiFi 名称和密码**：屏幕只支持 **2.4G**。路由器 2.4G 和 5G 分开叫两个名字的，要 2.4G 那个
- 屏幕现在插在这台电脑上了吗（刷固件要用 USB 线连核心板，见下面第二步）

## 第一步：部署服务端

拿到代码：`git clone https://github.com/Anson2048/kitchen-menu.git`；没有 git 就下载 `https://github.com/Anson2048/kitchen-menu/archive/refs/heads/main.zip` 解压。

### 有 Docker 就用 Docker

`docker compose version` 能跑就用：

```bash
cp .env.example .env      # 把 MENU_PUBLIC_URL 填成 http://<局域网IP>:8000
docker compose up -d --build
```

国内构建慢的镜像参数见 [docs/deploy.md](docs/deploy.md)。

### 没有 Docker 就直接跑

**不要为这个去装 Docker Desktop**（Windows 上要开 WSL2、要重启，对用户太重）。用 [uv](https://docs.astral.sh/uv/) 直接跑，Python 和依赖它会自己下：

- 装 uv：Windows `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`；macOS / Linux `curl -LsSf https://astral.sh/uv/install.sh | sh`；都不通就 `pip install uv`
- 在仓库根目录启动（`MENU_DATA_DIR` 是数据目录，菜单都存在里面，不要删）：

  ```bash
  uv run --python 3.12 --with "fastapi==0.115.*" --with "uvicorn==0.32.*" --with "pillow==11.*" \
    uvicorn app:app --host 0.0.0.0 --port 8000
  ```

  启动前要设的环境变量（字体是渲染屏幕用的，必须是本机有的中文字体）：

  | 系统 | `MENU_FONT_BOLD` | `MENU_FONT_REGULAR` | `MENU_FONT_INDEX` |
  | --- | --- | --- | --- |
  | Windows | `C:\Windows\Fonts\msyhbd.ttc` | `C:\Windows\Fonts\msyh.ttc` | `0` |
  | macOS | `/System/Library/Fonts/STHeiti Medium.ttc` | `/System/Library/Fonts/STHeiti Light.ttc` | `1` |
  | Linux | 装 `fonts-noto-cjk`，默认路径不用设 | | |

  另外设 `MENU_DATA_DIR=./data`、`MENU_PUBLIC_URL=http://<局域网IP>:8000`。

- **开机自启**，不然电脑一重启服务就没了：Windows 写一个 `.cmd`（设好上面的变量再跑 uv 那行）放进启动文件夹（`shell:startup`），不用管理员权限；macOS 用 launchd 的 `~/Library/LaunchAgents/*.plist`；Linux 用 systemd。做完告诉用户服务装在哪、怎么停
- **Windows 防火墙**：第一次启动会弹「允许访问」，让用户点允许（勾专用网络）。没弹或者点错了，用管理员权限加规则：`netsh advfirewall firewall add rule name="kitchen-menu" dir=in action=allow protocol=TCP localport=8000`。网络被设成「公用」时也会被挡

### 找对局域网 IP

要的是这台机器在家里 WiFi / 有线网里的地址，一般是 `192.168.x.x` 或 `10.x.x.x`。别选虚拟网卡和代理的：`172.17～31.x`（Docker、WSL）、`198.18.x.x`（代理软件的 TUN）、`100.x.x.x`（Tailscale）。

### 验证

- 本机：`curl http://<局域网IP>:8000/api/device` 回一段 JSON
- 第一次启动会在后台拉菜品库（三百多道菜，几分钟），日志里出现「xxx 道菜」就好了。GitHub 不通就在环境变量里设 `MENU_AUTO_IMPORT=0` 先跳过
- 让用户手机连同一个 WiFi 打开 `http://<局域网IP>:8000`，能打开再往下走。打不开先查防火墙和 IP

## 第二步：刷固件

硬件是合宙 ESP32-C3 核心板 + 7.5 寸 UC8179 三色屏，接线见 [docs/hardware.md](docs/hardware.md)。原理和细节见 [firmware/README.md](firmware/README.md)，下面只讲步骤。

1. 让用户用**能传数据的** USB 线连电脑和**核心板自己的 Type-C**。外壳侧面的 Type-C 只能充电，连那个电脑认不到
2. 进 `firmware/` 目录。**先告诉用户「等我说按键的时候，按一下屏幕下边框的白色按键」，再跑命令**：板子平时在深度睡眠，USB 不出现，脚本会等最多 3 分钟

   ```bash
   uv run flash.py --backup
   ```

   这一步把原厂固件整片备份到 `backup/`，并截出 `bootloader-orig.bin`。**不能跳过**：这块板只能用原厂 bootloader，PlatformIO 编的会无限重启。备份文件告诉用户留好，不要删、不要提交

3. 刷入。同样**先叫用户准备按键**再跑（备份完板子会重启，可能又睡着了）：

   ```bash
   uv run flash.py --ssid "<WiFi名>" --password "<WiFi密码>" --server http://<局域网IP>:8000
   ```

   第一次会下载编译工具链（几百 MB，几分钟），之后就快了。WiFi 名或密码里有空格、引号、`$`、`&`、`%` 的，按当前 shell 的规则转义好（PowerShell 里用单引号最省事），也可以改用环境变量 `MENU_WIFI_SSID` / `MENU_WIFI_PASS` / `MENU_SERVER`

刷完屏幕会自己重启、连 WiFi、找服务端，三色屏刷新要十几秒，会闪几下。

### 刷机出问题

- **3 分钟没等到串口**：换一根线（很多线只能充电）、确认插的是核心板的口、再按一次键。Windows 看设备管理器的「端口 (COM 和 LPT)」有没有新出现的 COM 口，有就用 `--port COMx` 指定
- **Linux 上没权限**：当前用户加进 `dialout`（或 `uucp`）组，重新登录
- **刷完一直重启、屏幕没反应**：多半是用了非原厂 bootloader。确认 `bootloader-orig.bin` 是 `--backup` 截出来的；恢复原厂用 `uv run --with "esptool>=5" esptool --chip esp32c3 -p <串口> write-flash 0x0 backup/orig-full-<时间>.bin`
- **进不了下载模式**：按住核心板背面的 BOOT 键，再按一下正面的键，松开 BOOT，再跑脚本

## 第三步：确认和交给用户

- 屏幕显示出当天的菜单（还没录菜会显示「还没有菜单」），并且 `curl http://<局域网IP>:8000/api/device` 返回里有 `seen`（屏幕上次联网时间）、`battery` 不再是 `null`，就成功了
- 屏幕右下角红字「未联网」、或者整屏英文提示 `Menu-Setup-XXXX`：WiFi 没连上（不是 2.4G、密码错）或者连不到服务端（IP 不对、防火墙）。改好后重新跑第 3 步刷入；也可以让用户手机连屏幕开的 `Menu-Setup-XXXX` 热点，在弹出的页面里改（没弹就打开 `http://192.168.4.1`）
- 最后告诉用户：
  - 手机连家里 WiFi 打开 `http://<局域网IP>:8000`，用浏览器的「添加到主屏幕」，以后像 App 一样点开
  - 先去「设置」里设好阿姨周几来、几点到几点走，屏幕会按这个切换日期
  - 屏幕每天到点自己醒来更新；想马上看到效果就按一下屏幕下边框的白色按键
  - 服务没有登录，只能在家里 WiFi 里用。要在外面用，看 [docs/deploy.md](docs/deploy.md) 的「安全」一节，别直接把端口开到公网

## 别做的事

- 别跳过备份，别往 0x0 刷 PlatformIO 编出来的 bootloader
- 别把 `.env`、`backup/`、`bootloader-orig.bin`、`data/` 提交到 git，也别上传到任何地方
- 别为了装服务端去开路由器端口映射
