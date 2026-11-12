# 部署服务端

服务端是一个 Python（FastAPI）程序加一个 SQLite 文件，放在家里任意一台常开的机器上：NAS、软路由、树莓派、旧电脑都行。屏幕和手机要能访问到它。

## Docker Compose（推荐）

```bash
git clone <本仓库> && cd kitchen-menu
cp .env.example .env          # 按需改，不改也能跑
docker compose up -d --build
```

打开 `http://<这台机器的 IP>:8000`。第一次启动会在后台从 GitHub 拉 HowToCook 菜品库（三百多道菜，几分钟），期间网页照常能用，拉完刷新就有了。日志：`docker compose logs -f menu`。

国内构建慢的话，给基础镜像、apt 和 pip 换镜像：

```bash
docker compose build --build-arg PY_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  --build-arg APT_MIRROR=mirrors.ustc.edu.cn --build-arg PIP_INDEX=https://mirrors.ustc.edu.cn/pypi/simple
```

GitHub 访问不了的话，在 `.env` 里设 `HOWTOCOOK_RAW` 换成镜像，或者先设 `MENU_AUTO_IMPORT=0` 跳过，只用自己加的菜。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 时区，决定屏幕几点醒、阿姨几点到 |
| `WAKE_TIMES` | `06:00,09:30,10:45` | 屏幕默认几点醒，网页设置里能改 |
| `SWITCH_TIME` | `18:00` | 几点后屏幕切到阿姨下一次来的日子，网页设置里能改 |
| `MENU_PUBLIC_URL` | 空 | 网页的访问地址（如 `http://192.168.1.10:8000`），通知里的链接和按钮用，网页设置里也能改 |
| `MENU_ADMIN_TOKEN` | 空 | 设了的话，上传固件要带这个 token |
| `MENU_AUTO_IMPORT` | `1` | 第一次启动自动导入 HowToCook，`0` 关掉 |
| `HOWTOCOOK_RAW` | GitHub raw 地址 | 导入菜品库时的下载地址，可以换镜像 |
| `MENU_DATA_DIR` | `/data` | 数据目录（Docker 里不用改，挂载 `./data` 就行） |
| `MENU_FONT_BOLD` / `MENU_FONT_REGULAR` / `MENU_FONT_INDEX` | Noto CJK | 渲染屏幕用的字体，不用 Docker 时按本机字体改 |

## 数据和备份

都在挂载的 `./data` 里：

| | |
| --- | --- |
| `menu.db` | SQLite：菜单、菜品库、设置、屏幕状态、日志、买过的东西、库存校正、周期任务 |
| `library/` | 菜品库导入源 `howtocook.json` 和菜的照片 `img/` |
| `firmware/` | 发布的固件 `firmware.bin` 和版本号 `version.txt` |

备份就是把 `data/` 整个复制走（`menu.db` 开了 WAL，停容器再复制最稳，或者用 `sqlite3 menu.db ".backup backup.db"`）。

更新菜品库：`docker compose exec menu python tools/import_howtocook.py /data/library && docker compose restart menu`，网页上改过的菜不会被覆盖。

## 不用 Docker

需要 Python 3.10+ 和一套中文字体：

```bash
pip install "fastapi==0.115.*" "uvicorn==0.32.*" "pillow==11.*"
MENU_DATA_DIR=./data MENU_FONT_BOLD=/path/to/NotoSansCJK-Bold.ttc MENU_FONT_REGULAR=/path/to/NotoSansCJK-Regular.ttc \
  uvicorn app:app --host 0.0.0.0 --port 8000
```

字体是 `.ttc` 时 `MENU_FONT_INDEX` 填简体中文在里面的序号（Noto CJK 是 2）。

## 安全

**服务没有登录**，任何能访问到它的人都能改菜单。只放在家里内网，或者：

- 用 Tailscale 等组网工具从外面访问
- 放在反向代理后面加认证（Caddy `basicauth`、Nginx `auth_basic` 等）。注意屏幕要访问 `/api/frame`、`/api/telemetry`、`/firmware.bin`，这三个路径要么放行、要么让屏幕走内网地址

屏幕走 https 时固件不校验证书（`setInsecure`），自签证书也能用。
