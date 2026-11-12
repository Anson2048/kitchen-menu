"""厨房墨水屏菜单服务。

家里请了阿姨做饭：每天的菜单在手机网页上录，厨房的墨水屏定时醒来拉图显示，阿姨看屏幕做菜。
每天的内容：这天阿姨来做什么（mode：cook 做饭 / cat 来但不做饭 / off 不来）、几道菜（dishes）、
要蒸的东西（steam）、给阿姨的留言（tasks）、备注（note），都是文字列表。阿姨哪几天来、几点到几点走在设置里改。

- 手机网页（/）录入；脚本和自动化用 /api/menu/<日期> 读写
- 菜品库：/api/dishes（HowToCook 导入 + 自己加的菜，带图、分类、原料、常吃次数）
- 快捷选项：/api/presets（蒸、留言、备注的常用项，按历史使用次数排）
- 买过的东西和库存：/api/purchases、/api/stock（外部导入订单，估家里还剩什么）
- 外部集成：/api/hooks/task 追加留言，/api/laundry 洗衣机洗完；发出的事件走设置里的 webhook 和通知
- 周期任务：/api/chores（擦楼梯每 2 周……），到期排进阿姨做饭的日子，屏幕上和留言一起显示
- 墨水屏醒来请求 /api/frame：带上电量、温湿度，拿到 800×480 黑红两层位图和下次醒来的秒数
- 数据在 MENU_DATA_DIR（默认 /data）：menu.db（SQLite），library/img 照片，firmware/ 固件，
  library/howtocook.json 是菜品库的导入源
"""
import base64
import calendar
import datetime as dt
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from PIL import Image, ImageDraw, ImageFont

DATA = Path(os.environ.get("MENU_DATA_DIR", "/data"))
DB = DATA / "menu.db"
LIB = DATA / "library"
IMG = LIB / "img"
FW_DIR = DATA / "firmware"
for p in (IMG, FW_DIR):
    p.mkdir(parents=True, exist_ok=True)

W, H = 800, 480
BAND = 40  # 屏幕按 40 行一段边下载边写入，省内存
LISTS = ["dishes", "steam", "tasks"]
WEEK = "一二三四五六日"
DEFAULT_WAKE = [t.strip() for t in os.environ.get("WAKE_TIMES", "06:00,09:30,10:45").split(",") if t.strip()]
DEFAULT_SWITCH = os.environ.get("SWITCH_TIME", "18:00")
# 阿姨的作息：周几来（0 = 周一）、几点到、几点走。到点算这天的菜用掉了（扣库存），走了之后的提醒排到下一个来的日子
SCHEDULE_DEFAULT = {"workdays": [0, 1, 2, 3, 4, 5], "arrive": "10:00", "leave": "12:00"}
MODES = ("cook", "cat", "off")  # 这天阿姨：做饭 / 来但不做饭 / 不来

# 电量分 0～4 格，0 = 该充电了。阈值按常见锂电池定，用一个放电周期后按 battery.csv 校准
BAT_STEPS = [3550, 3700, 3820, 3950]
CHARGING_MV = 4300  # 插着充电时读到的是充电板输出，明显高于满电

CATS = ["荤菜", "素菜", "水产", "汤粥", "主食", "自定义"]
DROP_CATS = {"早餐", "甜品", "饮品", "半成品", "调料"}  # 阿姨做的是正餐，这几类不进菜品库
STEAM_DEFAULT = ["鸡蛋", "南瓜", "红薯", "紫薯", "玉米", "山药", "芋头", "土豆", "苹果", "梨", "馒头", "包子", "花卷"]
TASK_DEFAULT = ["晒被子", "收被子", "晒衣服", "收衣服", "洗衣服", "叠衣服", "拖地", "擦桌子", "擦窗户", "换床单",
                "倒垃圾", "浇花", "洗水果", "切水果", "多煮点饭"]
NOTE_DEFAULT = ["3人份", "4人份", "少盐", "少油", "不要辣", "清淡点"]
# 采购清单里不列的常备调料
PANTRY = {"盐", "食用油", "油", "糖", "白糖", "酱油", "生抽", "老抽", "醋", "香醋", "陈醋", "料酒", "味精", "鸡精",
          "蚝油", "淀粉", "水淀粉", "玉米淀粉", "胡椒粉", "白胡椒粉", "花椒", "八角", "桂皮", "香叶", "水", "清水",
          "开水", "热水", "冰水", "葱", "姜", "蒜", "大蒜", "葱花", "姜片", "蒜末", "小葱", "香菜",
          "食用盐", "食盐", "精盐", "白砂糖", "冰糖", "香油", "麻油", "芝麻油", "生粉", "土豆淀粉", "植物油", "花生油",
          "菜籽油", "生抽酱油", "蒜头", "蒜瓣", "蒜蓉", "姜末", "生姜", "葱姜蒜", "大葱", "香葱", "老姜"}

FONT_BOLD = os.environ.get("MENU_FONT_BOLD", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
FONT_REG = os.environ.get("MENU_FONT_REGULAR", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
SC = int(os.environ.get("MENU_FONT_INDEX", "2"))  # Noto CJK 的 ttc 里简体中文是第 3 个字形
PUBLIC_URL = os.environ.get("MENU_PUBLIC_URL", "")  # 通知里的链接和按钮用；设置页也能改
ADMIN_TOKEN = os.environ.get("MENU_ADMIN_TOKEN", "")  # 设了就要求上传固件时带 Authorization: Bearer <token>
WHITE, BLACK, RED = (255, 255, 255), (0, 0, 0), (255, 0, 0)

app = FastAPI()
_fonts = {}


def font(size, bold=True):
    key = (size, bold)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size, index=SC)
    return _fonts[key]


def now():
    return dt.datetime.now()


def hm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def read_json(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


# ---------- 数据库 ----------
# 列表类字段（菜、蒸、留言、原料）存 JSON 文字。menus.auto 是留言里哪几条是自动化加的（网页上带标签，屏幕上不区分）。edits 是每次保存菜单（分析改菜单的习惯），battery 是每次醒来的电压，
# events 是屏幕每次请求（只留最近 3000 条）。dishes.edited = 网页上改过的自带菜，重新导入 HowToCook 时不覆盖
# purchases 是买过的东西，不分渠道（外部导入、手输、截图识别……），(channel, ref, name) 去重，ref 是渠道里的订单号，没有就用时间；
# aliases 把商品名（「一点小葱 约25g」）对到原料（「小葱」）和品类，跨渠道共用，没对上的 ingredient 为空
# chores 是周期任务：每 every 个 unit（d 天 / w 周 / m 月）做一次，last 上次做，next 排到哪天（那天过了就算做过），skip 是点过「这次不做」的日子

SCHEMA = """
CREATE TABLE IF NOT EXISTS menus (date TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'cook', dishes TEXT NOT NULL,
  steam TEXT NOT NULL, tasks TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated TEXT, auto TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS dishes (id TEXT PRIMARY KEY, name TEXT NOT NULL, cat TEXT NOT NULL, img TEXT,
  desc TEXT NOT NULL DEFAULT '', difficulty INTEGER, ingredients TEXT NOT NULL DEFAULT '[]', qty TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, edited INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS dishes_name ON dishes(name);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edits (id INTEGER PRIMARY KEY, time TEXT, menu_date TEXT, client TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS battery (id INTEGER PRIMARY KEY, time TEXT, mv INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT, type TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY, time TEXT NOT NULL, channel TEXT NOT NULL,
  ref TEXT NOT NULL, name TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '', count REAL NOT NULL DEFAULT 1, price REAL,
  raw TEXT NOT NULL DEFAULT '{}', created TEXT, UNIQUE(channel, ref, name));
CREATE INDEX IF NOT EXISTS purchases_time ON purchases(time);
CREATE TABLE IF NOT EXISTS aliases (name TEXT PRIMARY KEY, ingredient TEXT NOT NULL DEFAULT '',
  cat TEXT NOT NULL DEFAULT '', updated TEXT);
CREATE TABLE IF NOT EXISTS stock_adjust (id INTEGER PRIMARY KEY, time TEXT NOT NULL, ingredient TEXT NOT NULL,
  amount REAL NOT NULL, reason TEXT NOT NULL DEFAULT 'fix');
CREATE TABLE IF NOT EXISTS chores (id INTEGER PRIMARY KEY, name TEXT NOT NULL, every INTEGER NOT NULL,
  unit TEXT NOT NULL DEFAULT 'w', last TEXT NOT NULL, next TEXT, skip TEXT NOT NULL DEFAULT '[]');
"""


@contextmanager
def db():
    """一次请求开一个连接，出了 with 自动提交并关闭。"""
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def kv_get(key, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(key, value, c=None):
    sql = "INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    if c:
        return c.execute(sql, (key, dumps(value)))
    with db() as c:
        c.execute(sql, (key, dumps(value)))


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        if "auto" not in {r[1] for r in c.execute("PRAGMA table_info(menus)")}:  # 2026-11-11 加的列
            c.execute("ALTER TABLE menus ADD COLUMN auto TEXT NOT NULL DEFAULT '[]'")
    if not (LIB / "howtocook.json").exists() and os.environ.get("MENU_AUTO_IMPORT", "1") != "0":
        threading.Thread(target=first_import, daemon=True).start()
    sync_howtocook()


def first_import():
    """第一次启动还没有菜品库：后台从 GitHub 拉 HowToCook，拉完同步进数据库（几分钟，期间网页照常能用）。"""
    import subprocess
    import sys
    tool = Path(__file__).parent / "tools" / "import_howtocook.py"
    print("菜品库为空，开始从 HowToCook 导入……", flush=True)
    r = subprocess.run([sys.executable, str(tool), str(LIB)], capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip()[-500:], flush=True)
    sync_howtocook()


def sync_howtocook():
    """howtocook.json（tools/import_howtocook.py 生成）更新过就同步进 dishes，网页上改过的菜不动。"""
    p = LIB / "howtocook.json"
    if not p.exists() or kv_get("howtocook_mtime") == p.stat().st_mtime:
        return
    with db() as c:
        for d in read_json(p, []):
            c.execute("""INSERT INTO dishes (id, name, cat, img, desc, difficulty, ingredients, qty, source)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'howtocook')
                         ON CONFLICT(id) DO UPDATE SET name = excluded.name, cat = excluded.cat, img = excluded.img,
                           desc = excluded.desc, difficulty = excluded.difficulty, ingredients = excluded.ingredients,
                           qty = excluded.qty WHERE dishes.edited = 0""",
                      (d["id"], d["name"], d["cat"], d.get("img"), d.get("desc") or "", d.get("difficulty"),
                       dumps(d.get("ingredients") or []), dumps(d.get("qty") or [])))
        kv_set("howtocook_mtime", p.stat().st_mtime, c)


# ---------- 事件：webhook 和通知 ----------
# 服务里发生的事都走 emit()：webhook 收到全部勾选的事件（给 HA 等自动化用），通知只推勾选的几种到手机。
# 通知渠道：ntfy（url 填 https://ntfy.sh/<主题>，自建的同理，token 可选）、Bark（url 填 https://api.day.app/<key>）。
# 事件的 data 字段随事件不同，见 docs/integrations.md。

EVENTS = {
    "menu_saved": "菜单改了",
    "device_seen": "屏幕醒来联网",
    "device_missed": "屏幕没按时醒来",
    "battery_low": "屏幕该充电了",
    "menu_missing": "阿姨下次来的菜还没录",
    "laundry": "洗衣机洗完，问要不要加晒衣服",
}
NOTIFY_DEFAULT = {"kind": "", "url": "", "token": "", "events": ["device_missed", "battery_low", "menu_missing", "laundry"],
                  "remind_at": "20:00"}  # remind_at：前一天几点还没录菜就提醒
WEBHOOK_DEFAULT = {"url": "", "events": list(EVENTS)}


def _post(url, body, headers=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"content-type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def send_notify(cfg, title, message, action=None, link=None):
    """推一条通知。action = (按钮文字, 本服务的路径, POST 的 JSON)，ntfy 上是按钮，点了直接调；Bark 只能点开链接。"""
    kind, url = cfg.get("kind"), (cfg.get("url") or "").strip().rstrip("/")
    if not kind or not url:
        return False
    base = settings()["public_url"]
    if kind == "ntfy":
        server, _, topic = url.rpartition("/")
        body = {"topic": topic, "title": title, "message": message}
        if base:
            body["click"] = link or base
            if action:
                body["actions"] = [{"action": "http", "label": action[0], "url": base + action[1], "method": "POST",
                                    "headers": {"content-type": "application/json"},
                                    "body": json.dumps(action[2], ensure_ascii=False), "clear": True}]
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
        _post(server, body, headers)
    elif kind == "bark":
        body = {"title": title, "body": message, "group": "厨房菜单"}
        if base:
            body["url"] = link or base
        _post(url, body)
    else:
        return False
    return True


def emit(event, title, message, data=None, action=None):
    """发一个事件：后台线程里推 webhook 和通知，失败只记日志，不影响接口返回。"""
    st = settings()
    payload = {"event": event, "time": now().isoformat(timespec="seconds"), "title": title, "message": message,
               "data": data or {}}

    def run():
        wh = st["webhook"]
        if wh.get("url") and event in wh.get("events", []):
            try:
                _post(wh["url"], payload)
            except Exception as e:
                log_event({"type": "webhook_error", "event": event, "error": str(e)[:200]})
        nt = st["notify"]
        if event in nt.get("events", []):
            try:
                send_notify(nt, title, message, action)
            except Exception as e:
                log_event({"type": "notify_error", "event": event, "error": str(e)[:200]})

    threading.Thread(target=run, daemon=True).start()


def check_alerts():
    """定时检查（每 5 分钟）：屏幕没按时醒、下次阿姨来的菜还没录。同一件事只提醒一次，记在 kv 的 alerts 里。"""
    t, st = now(), device_state()
    seen = kv_get("alerts", {})
    changed = False
    # 屏幕说好几点醒，过了 30 分钟还没来
    nw = st.get("next_wake")
    if nw and st.get("seen") and st["seen"] < nw and t > dt.datetime.fromisoformat(nw) + dt.timedelta(minutes=30) \
            and seen.get("device_missed") != nw:
        emit("device_missed", "屏幕没按时醒来", f"本该 {nw[5:16].replace('T', ' ')} 醒来，上次联网是 "
             f"{st['seen'][5:16].replace('T', ' ')}。看看是不是没电了，或者按一下屏幕前面的键",
             {"expected": nw, "last_seen": st["seen"]})
        seen["device_missed"] = nw
        changed = True
    # 阿姨下次来做饭的日子，前一天到点了还没录菜
    d = visit_day(t)
    remind = hm(settings()["notify"].get("remind_at") or "20:00")
    if d > t.date() and day_mode(d) == "cook" and t >= dt.datetime.combine(d - dt.timedelta(days=1), remind) \
            and not (load_menu(d) or {}).get("dishes") and seen.get("menu_missing") != d.isoformat():
        emit("menu_missing", "菜还没录", f"阿姨{day_label(d)}来做饭，菜单还是空的", {"date": d.isoformat()},
             )
        seen["menu_missing"] = d.isoformat()
        changed = True
    if changed:
        kv_set("alerts", seen)


@app.on_event("startup")
async def start_checker():
    import asyncio

    async def loop():
        while True:
            await asyncio.sleep(300)
            try:
                await asyncio.to_thread(check_alerts)
            except Exception as e:
                print("check_alerts:", e)

    asyncio.create_task(loop())


@app.post("/api/notify/test")
async def notify_test(request: Request):
    """设置页「发一条试试」：用传来的配置（还没保存的也行）推一条。"""
    body = await request.json() if (await request.body()) else {}
    cfg = {**settings()["notify"], **body}
    try:
        ok = send_notify(cfg, "厨房菜单", "通知配置成功 👍")
    except Exception as e:
        raise HTTPException(502, f"发送失败：{e}")
    if not ok:
        raise HTTPException(400, "先选通知渠道并填地址")
    return {"ok": True}


@app.post("/api/webhook/test")
async def webhook_test(request: Request):
    body = await request.json() if (await request.body()) else {}
    url = body.get("url") or settings()["webhook"].get("url")
    if not url:
        raise HTTPException(400, "先填 webhook 地址")
    try:
        code = _post(url, {"event": "test", "time": now().isoformat(timespec="seconds"), "title": "测试",
                           "message": "webhook 配置成功", "data": {}})
    except Exception as e:
        raise HTTPException(502, f"发送失败：{e}")
    return {"ok": True, "status": code}


# ---------- 设置 ----------

def wake_slots(day: dt.date = None):
    """屏幕实际醒来的时间：手动设的 + 切换时间（到点自动醒一次，切到明天）。
    阿姨不来的日子白天不醒，只在切换时间醒一次，把下一个来的日子拉过来。"""
    st = settings()
    if day and day_mode(day) == "off":
        return [st["switch_time"]]
    return sorted(set(st["wake_times"]) | {st["switch_time"]})


def settings():
    st = kv_get("settings", {})
    pre = st.get("presets") or {}
    return {
        "presets": {"steam": pre.get("steam", STEAM_DEFAULT), "tasks": pre.get("tasks", TASK_DEFAULT),
                    "note": pre.get("note", NOTE_DEFAULT)},
        "auto_sort": {**{"steam": False, "tasks": False, "note": False}, **(st.get("auto_sort") or {})},
        "wake_times": st.get("wake_times", DEFAULT_WAKE),
        "switch_time": st.get("switch_time", DEFAULT_SWITCH),
        "schedule": {**SCHEDULE_DEFAULT, **(st.get("schedule") or {})},
        "notify": {**NOTIFY_DEFAULT, **(st.get("notify") or {})},
        "webhook": {**WEBHOOK_DEFAULT, **(st.get("webhook") or {})},
        "public_url": st.get("public_url") or PUBLIC_URL,
        "events": EVENTS,
        "hidden": hidden_ids(),
    }


def hidden_ids():
    with db() as c:
        return [r[0] for r in c.execute("SELECT id FROM dishes WHERE hidden = 1 ORDER BY id")]


@app.get("/api/settings")
def get_settings():
    return settings()


@app.put("/api/settings")
async def put_settings(request: Request):
    """只传要改的字段，比如 {"wake_times": ["06:00","11:00"]}。"""
    body = await request.json()
    st = kv_get("settings", {})
    if "hidden" in body:
        with db() as c:
            c.execute("UPDATE dishes SET hidden = 0")
            c.executemany("UPDATE dishes SET hidden = 1 WHERE id = ?", [(x,) for x in body["hidden"]])
    for k in ("presets", "auto_sort", "wake_times", "switch_time", "schedule", "notify", "webhook", "public_url"):
        if k in body:
            v = body[k]
            if k == "wake_times":
                v = sorted({hm(x).strftime("%H:%M") for x in v})

            if k == "switch_time":
                v = hm(v).strftime("%H:%M")
            if k == "schedule":
                v = {**(st.get(k) or {}), **v}
                if "workdays" in v:
                    v["workdays"] = sorted({int(x) for x in v["workdays"] if 0 <= int(x) <= 6})
                for t in ("arrive", "leave"):
                    if t in v:
                        v[t] = hm(v[t]).strftime("%H:%M")
            if k in ("notify", "webhook"):
                v = {**(st.get(k) or {}), **v}
                if "events" in v:
                    v["events"] = [e for e in v["events"] if e in EVENTS]
            if k == "public_url":
                v = (v or "").strip().rstrip("/")
            if k in ("presets", "auto_sort"):
                v = {**(st.get(k) or {}), **v}
            st[k] = v
    sw = st.get("switch_time", DEFAULT_SWITCH)
    if "wake_times" in st:
        st["wake_times"] = [x for x in st["wake_times"] if x != sw]
    kv_set("settings", st)
    return get_settings()


# ---------- 菜单 ----------

def check_date(date):
    try:
        return dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "日期格式是 YYYY-MM-DD")


def menu_row(r):
    return {"date": r["date"], "mode": r["mode"], **{k: json.loads(r[k]) for k in LISTS}, "note": r["note"],
            "updated": r["updated"], "auto": json.loads(r["auto"])}


def load_menu(day: dt.date):
    with db() as c:
        r = c.execute("SELECT * FROM menus WHERE date = ?", (day.isoformat(),)).fetchone()
    return menu_row(r) if r else None


def save_menu(m, c):
    auto = [x for x in m.get("auto") or [] if x in (m.get("tasks") or [])]  # 留言删了或改了，标签跟着没
    c.execute("INSERT OR REPLACE INTO menus (date, mode, dishes, steam, tasks, note, updated, auto) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (m["date"], m["mode"], *(dumps(m.get(k) or []) for k in LISTS), m["note"], m["updated"], dumps(auto)))


def schedule():
    return settings()["schedule"]


def day_mode(day: dt.date):
    """这天阿姨来做什么。设置里不来的星期几固定是 off。"""
    if day.weekday() not in schedule()["workdays"]:
        return "off"
    return (load_menu(day) or {}).get("mode", "cook")


def next_visit(day: dt.date):
    """day 之后阿姨下一次来的日子（做饭或来了不做饭）。"""
    for i in range(1, 15):
        d = day + dt.timedelta(days=i)
        if day_mode(d) != "off":
            return d
    return day + dt.timedelta(days=1)


def all_menus():
    with db() as c:
        return [menu_row(r) for r in c.execute("SELECT * FROM menus ORDER BY date")]


def clean_menu(body):
    out = {}
    for k in LISTS:
        items = body.get(k) or []
        if isinstance(items, str):
            items = items.splitlines()
        seen, clean = set(), []
        for x in items:
            x = (x or "").strip()
            if x and x not in seen:
                seen.add(x)
                clean.append(x)
        out[k] = clean
    out["note"] = (body.get("note") or "").strip()
    out["mode"] = body.get("mode") if body.get("mode") in MODES else "cook"
    out["auto"] = [x for x in body.get("auto") or [] if x in out["tasks"]]
    return out


@app.get("/api/menu/{date}")
def get_menu(date: str):
    day = check_date(date)
    m = load_menu(day) or empty_menu(day)
    return extras(m)


def empty_menu(day):
    return {"date": day.isoformat(), "dishes": [], "steam": [], "tasks": [], "note": "", "mode": "cook", "updated": None,
            "auto": []}


@app.put("/api/menu/{date}")
async def put_menu(date: str, request: Request):
    day = check_date(date)
    menu = clean_menu(await request.json())
    menu["date"] = day.isoformat()
    menu["updated"] = now().isoformat(timespec="seconds")
    client = "web" if "Mozilla" in request.headers.get("user-agent", "") else "api"
    with db() as c:
        save_menu(menu, c)
        log_edit(menu, client, c)
    emit("menu_saved", "菜单改了", f"{day_label(day)}：{'、'.join(menu['dishes']) or '没有菜'}", menu)
    return extras(menu)


def log_edit(menu, client, c):
    summary = {"cook": "", "cat": "[不做饭]", "off": "[不来]"}[menu["mode"]] + "、".join(menu["dishes"])
    c.execute("INSERT INTO edits (time, menu_date, client, summary) VALUES (?, ?, ?, ?)",
              (menu["updated"], menu["date"], client, summary[:60]))


@app.post("/api/menu/{date}/tasks")
async def add_task(date: str, request: Request):
    """追加一条留言（不动别的内容），自动化用。auto=true 时网页上带「自动」标签。重复加不会出两条。"""
    day = check_date(date)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    m = load_menu(day) or empty_menu(day)
    if text not in m["tasks"]:
        m["tasks"].append(text)
        if body.get("auto"):
            m["auto"].append(text)
    m["updated"] = now().isoformat(timespec="seconds")
    with db() as c:
        save_menu(m, c)
        log_edit(m, body.get("source") or "auto", c)
        lw = kv_get("laundry")
        if lw and lw.get("date") == day.isoformat() and lw.get("state") == "pending" and text == lw.get("text"):
            kv_set("laundry", {**lw, "state": "added"}, c)
    return extras(m)


@app.post("/api/hooks/task")
async def hook_task(request: Request):
    """外部自动化加一条留言：{"text": "收快递", "date": 可选}。不给日期就加到阿姨最近一次来的日子（今天她还没走就是今天）。"""
    body = await request.json()
    d = check_date(body["date"]) if body.get("date") else visit_day(now())
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    m = load_menu(d) or empty_menu(d)
    if text not in m["tasks"]:
        m["tasks"].append(text)
        if body.get("auto", True):
            m["auto"].append(text)
    m["updated"] = now().isoformat(timespec="seconds")
    with db() as c:
        save_menu(m, c)
        log_edit(m, body.get("source") or "hook", c)
    return {"date": d.isoformat(), "label": day_label(d), "tasks": m["tasks"]}


# ---------- 洗衣机提醒 ----------
# HA 之类在洗衣机洗完时调 /api/laundry，这里记下来并算出该提醒哪天，发 laundry 事件（通知里带「加上」按钮）。
# 没点通知的话，网页上那天的留言下面会出一条建议，点一下也能加（或者忽略）。

LAUNDRY_TEXT = "晒衣服"


def visit_day(t: dt.datetime):
    """t 时刻往后，阿姨最近一次来的日子：今天她来、还没走就是今天，否则是下一个来的日子。"""
    d = t.date()
    if day_mode(d) != "off" and t.strftime("%H:%M") < schedule()["leave"]:
        return d
    return next_visit(d)


laundry_day = visit_day


def suggestions(m):
    lw = kv_get("laundry")
    if not lw or lw.get("state") != "pending" or lw.get("date") != m["date"] or lw["text"] in m["tasks"]:
        return []
    return [{"kind": "laundry", "text": lw["text"], "reason": f"洗衣机 {fmt_done(lw['done_at'])} 洗好了"}]


def fmt_done(s):
    t, today = dt.datetime.fromisoformat(s), now().date()
    pre = {0: "", -1: "昨天 "}.get((t.date() - today).days, f"{t.month}月{t.day}日 ")
    return pre + t.strftime("%H:%M")


@app.post("/api/laundry")
async def laundry_done(request: Request):
    """洗衣机洗完（HA 调）。返回提醒哪天和推送文案。"""
    body = await request.json() if (await request.body()) else {}
    t = dt.datetime.fromisoformat(body["done_at"]) if body.get("done_at") else now()
    if t.tzinfo:
        t = t.astimezone().replace(tzinfo=None)  # 换成本地时间（容器时区看 TZ）
    d = laundry_day(t)
    lw = {"done_at": t.isoformat(timespec="seconds"), "date": d.isoformat(), "text": LAUNDRY_TEXT, "state": "pending"}
    if LAUNDRY_TEXT in (load_menu(d) or {}).get("tasks", []):
        lw["state"] = "added"  # 已经写了晒衣服，不用再问
    kv_set("laundry", lw)
    label = day_label(d)
    msg = f"{fmt_done(lw['done_at'])} 洗好了。给阿姨{label}的留言加「{LAUNDRY_TEXT}」？"
    if lw["state"] == "pending":
        emit("laundry", "洗衣机洗完了", msg, {"date": d.isoformat(), "text": LAUNDRY_TEXT},
             action=("加上", f"/api/menu/{d.isoformat()}/tasks", {"text": LAUNDRY_TEXT, "auto": True}))
    return {**lw, "label": label, "need_ask": lw["state"] == "pending", "message": msg}


def day_label(d: dt.date):
    rel = {0: "今天", 1: "明天", 2: "后天"}.get((d - now().date()).days, "")
    return f"{rel}（{d.month}月{d.day}日 周{WEEK[d.weekday()]}）" if rel else f"{d.month}月{d.day}日（周{WEEK[d.weekday()]}）"


@app.post("/api/laundry/dismiss")
def laundry_dismiss():
    lw = kv_get("laundry")
    if lw and lw.get("state") == "pending":
        kv_set("laundry", {**lw, "state": "dismissed"})
    return {"ok": True}


def extras(m):
    """菜单接口一起返回的：洗衣机建议、这天排到的周期任务（不存在 tasks 里，网页上单独显示）。"""
    return {**m, "suggest": suggestions(m), "chores": chores_on(m["date"])}


# ---------- 周期任务 ----------
# 擦楼梯、洗沙发套这类：上次做 + 间隔 = 到期，落到之后阿姨第一个做饭的日子（来了不做饭的日子不派），每天最多一个，
# 其余往后顺延。排到的那天过了就当做过了。不写进 menus.tasks，渲染屏幕和网页时临时加上。

UNITS = {"d": "天", "w": "周", "m": "个月"}


def add_interval(d: dt.date, every, unit):
    if unit == "d":
        return d + dt.timedelta(days=every)
    if unit == "w":
        return d + dt.timedelta(weeks=every)
    m = d.month - 1 + every
    y, m = d.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def plan_chores():
    """重新排一遍，返回所有周期任务（带 next）。"""
    t = now()
    today = t.date()
    with db() as c:
        rows = [dict(r, skip=json.loads(r["skip"])) for r in c.execute("SELECT * FROM chores ORDER BY id")]
        for r in rows:
            if r["next"] and r["next"] < today.isoformat():  # 排到的日子过了 = 做过了
                r["last"], r["next"] = r["next"], None
            r["skip"] = [x for x in r["skip"] if x >= today.isoformat()]
        modes, taken = {}, set()

        def free(d, r):
            if d not in modes:
                modes[d] = day_mode(d)
            return modes[d] == "cook" and d not in taken and d.isoformat() not in r["skip"]

        # 今天已经排上的不挪（中午之后再重排会挪到明天）
        for r in rows:
            if r["next"] == today.isoformat() and free(today, r):
                taken.add(today)
                r["fixed"] = True
        # 阿姨走了之后，新排的从明天起
        start = today if t.strftime("%H:%M") < schedule()["leave"] else today + dt.timedelta(days=1)
        for r in sorted(rows, key=lambda r: (add_interval(dt.date.fromisoformat(r["last"]), r["every"], r["unit"]), r["id"])):
            if r.pop("fixed", False):
                continue
            d = max(add_interval(dt.date.fromisoformat(r["last"]), r["every"], r["unit"]), start)
            for _ in range(120):
                if free(d, r):
                    break
                d += dt.timedelta(days=1)
            taken.add(d)
            r["next"] = d.isoformat()
        for r in rows:
            c.execute("UPDATE chores SET last = ?, next = ?, skip = ? WHERE id = ?",
                      (r["last"], r["next"], dumps(r["skip"]), r["id"]))
    for r in rows:
        r["label"] = f"每 {r['every']} {UNITS[r['unit']]}"
    return rows


def chores_on(date):
    return [{"id": r["id"], "name": r["name"], "label": r["label"], "last": r["last"]}
            for r in plan_chores() if r["next"] == date]


def clean_chore(body):
    name = (body.get("name") or "").strip()
    try:
        every = int(body.get("every") or 0)
    except ValueError:
        every = 0
    unit = body.get("unit") if body.get("unit") in UNITS else "w"
    if not name or every < 1:
        raise HTTPException(400, "要有名称，间隔至少 1")
    last = check_date(body.get("last") or now().date().isoformat()).isoformat()
    return name, every, unit, last


@app.get("/api/chores")
def list_chores():
    return plan_chores()


@app.post("/api/chores")
async def add_chore(request: Request):
    """新增：{"name": "擦楼梯", "every": 2, "unit": "w", "last": "2026-11-01"}，last 不填就是今天。"""
    with db() as c:
        c.execute("INSERT INTO chores (name, every, unit, last) VALUES (?, ?, ?, ?)", clean_chore(await request.json()))
    return plan_chores()


@app.put("/api/chores/{cid}")
async def edit_chore(cid: int, request: Request):
    with db() as c:
        c.execute("UPDATE chores SET name = ?, every = ?, unit = ?, last = ?, next = NULL WHERE id = ?",
                  (*clean_chore(await request.json()), cid))
    return plan_chores()


@app.delete("/api/chores/{cid}")
def delete_chore(cid: int):
    with db() as c:
        c.execute("DELETE FROM chores WHERE id = ?", (cid,))
    return plan_chores()


@app.post("/api/chores/{cid}/skip")
async def skip_chore(cid: int, request: Request):
    """这天不做，顺延到阿姨下一个做饭的日子：{"date": "2026-11-15"}"""
    day = check_date((await request.json()).get("date") or "").isoformat()
    with db() as c:
        r = c.execute("SELECT skip FROM chores WHERE id = ?", (cid,)).fetchone()
        if not r:
            raise HTTPException(404)
        c.execute("UPDATE chores SET skip = ?, next = NULL WHERE id = ?", (dumps(sorted({*json.loads(r[0]), day})), cid))
    return plan_chores()


@app.delete("/api/menu/{date}")
def delete_menu(date: str):
    with db() as c:
        c.execute("DELETE FROM menus WHERE date = ?", (check_date(date).isoformat(),))
    return {"ok": True}


@app.get("/api/menus")
def list_menus(days: int = 30):
    return list(reversed(all_menus()))[:days]


# ---------- 菜品库 ----------

def dish_row(r):
    return {"id": r["id"], "name": r["name"], "cat": r["cat"], "img": r["img"], "desc": r["desc"],
            "difficulty": r["difficulty"], "ingredients": json.loads(r["ingredients"]), "qty": json.loads(r["qty"]),
            "source": r["source"], "hidden": bool(r["hidden"])}


def library():
    """HowToCook + 自己加的菜（rowid 顺序：先自带的，后自己加的）。"""
    with db() as c:
        rows = c.execute(f"SELECT * FROM dishes WHERE cat NOT IN ({', '.join('?' * len(DROP_CATS))}) ORDER BY rowid",
                         tuple(DROP_CATS)).fetchall()
    return [dish_row(r) for r in rows]


def usage():
    """每道菜/每个选项用过几次、最近一次哪天。"""
    stats = {k: Counter() for k in LISTS + ["note"]}
    last = {}
    for m in all_menus():
        for k in LISTS:
            for x in m.get(k, []):
                stats[k][x] += 1
                last[x] = m.get("date")
        if m.get("note"):
            for x in re.split(r"[，,、；;\s]+", m["note"]):
                if x:
                    stats["note"][x] += 1
    return stats, last


def save_photo(data_url, name):
    m = re.match(r"data:image/[\w+]+;base64,(.+)", data_url or "", re.S)
    if not m:
        raise HTTPException(400, "照片格式不对")
    im = Image.open(io.BytesIO(base64.b64decode(m.group(1)))).convert("RGB")
    im.thumbnail((400, 400))
    fn = f"{name}-{int(time.time())}.jpg"
    im.save(IMG / fn, "JPEG", quality=75, optimize=True)
    return fn


@app.get("/api/dishes")
def list_dishes():
    stats, last = usage()
    out = []
    for d in library():
        out.append({"id": d["id"], "name": d["name"], "cat": d["cat"], "hidden": d["hidden"],
                    "img": f"/img/{d['img']}" if d.get("img") else None,
                    "desc": d.get("desc", ""), "difficulty": d.get("difficulty"),
                    "ingredients": [i["name"] for i in d.get("ingredients", []) if not i.get("optional")],
                    "custom": d.get("source") == "custom",
                    "count": stats["dishes"][d["name"]], "last": last.get(d["name"])})
    # 菜单里出现过、但库里没有的菜，也列出来（归到自定义），方便下次直接点
    names = {d["name"] for d in out}
    for name, n in stats["dishes"].items():
        if name not in names:
            out.append({"id": "h-" + hashlib.sha1(name.encode()).hexdigest()[:8], "name": name, "cat": "自定义", "hidden": False,
                        "img": None, "desc": "", "difficulty": None, "ingredients": [], "custom": False,
                        "count": n, "last": last.get(name)})
    return {"cats": CATS, "dishes": out}


@app.get("/api/dishes/{did}")
def get_dish(did: str):
    for d in library():
        if d["id"] == did:
            return d
    raise HTTPException(404)


@app.post("/api/dishes")
async def add_dish(request: Request):
    """自己加菜：{name, cat, ingredients: [..] 或多行文字, photo: dataURL（可选）}"""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "菜名不能为空")
    if any(d["name"] == name for d in library()):
        raise HTTPException(409, "已经有这道菜了")
    ing = body.get("ingredients") or []
    if isinstance(ing, str):
        ing = re.split(r"[\n，,、]+", ing)
    did = "c-" + hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:8]
    dish = {"id": did, "name": name, "cat": body.get("cat") or "自定义", "img": None, "desc": "",
            "difficulty": None, "ingredients": [{"name": x.strip(), "optional": False} for x in ing if x.strip()],
            "qty": [], "source": "custom"}
    if body.get("photo"):
        dish["img"] = save_photo(body["photo"], did)
    with db() as c:
        c.execute("INSERT INTO dishes (id, name, cat, img, ingredients, source) VALUES (?, ?, ?, ?, ?, 'custom')",
                  (did, name, dish["cat"], dish["img"], dumps(dish["ingredients"])))
    return dish


@app.put("/api/dishes/{did}/photo")
async def set_photo(did: str, request: Request):
    """给任意一道菜换/补照片：{photo: dataURL}"""
    if not any(d["id"] == did for d in library()):
        raise HTTPException(404)
    fn = save_photo((await request.json()).get("photo"), did)
    with db() as c:
        c.execute("UPDATE dishes SET img = ?, edited = 1 WHERE id = ?", (fn, did))
    return {"img": f"/img/{fn}"}


@app.put("/api/dishes/{did}")
async def edit_dish(did: str, request: Request):
    """改菜：{name, cat, ingredients, hidden} 只传要改的。改名会同步改历史菜单里的菜名。"""
    body = await request.json()
    lib = library()
    dish = next((d for d in lib if d["id"] == did), None)
    if not dish:
        raise HTTPException(404)
    if "hidden" in body:
        with db() as c:
            c.execute("UPDATE dishes SET hidden = ? WHERE id = ?", (int(bool(body["hidden"])), did))
    change = {}
    if body.get("name") and body["name"].strip() != dish["name"]:
        new = body["name"].strip()
        if any(d["name"] == new for d in lib):
            raise HTTPException(409, "已经有这道菜了")
        change["name"] = new
    if body.get("cat"):
        change["cat"] = body["cat"]
    if "ingredients" in body:
        ing = body["ingredients"]
        if isinstance(ing, str):
            ing = re.split(r"[\n，,、]+", ing)
        change["ingredients"] = [{"name": x.strip(), "optional": False} for x in ing if x.strip()]
    if change:
        if "ingredients" in change:
            change["ingredients"] = dumps(change["ingredients"])
        with db() as c:
            c.execute(f"UPDATE dishes SET {', '.join(k + ' = ?' for k in change)}, edited = 1 WHERE id = ?",
                      (*change.values(), did))
            if "name" in change:
                for m in map(menu_row, c.execute("SELECT * FROM menus").fetchall()):
                    if dish["name"] in m["dishes"]:
                        m["dishes"] = [change["name"] if x == dish["name"] else x for x in m["dishes"]]
                        save_menu(m, c)
    return get_dish(did)


@app.delete("/api/dishes/{did}")
def delete_dish(did: str):
    with db() as c:
        if not c.execute("DELETE FROM dishes WHERE id = ? AND source = 'custom'", (did,)).rowcount:
            raise HTTPException(400, "只能删自己加的菜")
    return {"ok": True}


@app.get("/img/{fn}")
def img(fn: str):
    p = IMG / Path(fn).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=2592000"})


@app.get("/api/presets")
def presets():
    stats, _ = usage()
    st = settings()
    out = {}
    for key in ("steam", "tasks", "note"):
        items = list(st["presets"][key])
        if st["auto_sort"][key]:
            items.sort(key=lambda x: -stats[key][x])
        out[key] = items
    return out


@app.get("/api/shopping/{date}")
def shopping(date: str):
    """这天要买什么：按菜品库的原料汇总（去掉常备调料），加上要蒸的东西。第 2 步自动买菜用。"""
    day = check_date(date)
    if day_mode(day) != "cook":  # 来了不做饭、不来的日子不买菜
        return {"date": date, "dishes": [], "need": []}
    m = load_menu(day) or {}
    lib = {d["name"]: d for d in library()}
    dishes, need = [], []
    for name in m.get("dishes", []):
        d = lib.get(name)
        ing = dish_ingredients(d)
        dishes.append({"name": name, "matched": bool(d), "ingredients": ing})
        need += [x for x in ing if x not in PANTRY]
    need += m.get("steam", [])
    need = list(dict.fromkeys(need))
    # 对着估的库存（这天阿姨来之前）看哪些家里有；调料按常备算
    stock = estimate_stock(dt.datetime.combine(day, arrive_time()) - dt.timedelta(minutes=1))
    al = alias_map()
    have = [x for x in need if pantry_like(x, al) or any(stock["left"].get(i, 0) > 0 for i in resolve(x, al))]
    return {"date": date, "dishes": dishes, "need": need, "have": have, "missing": [x for x in need if x not in have]}


def dish_ingredients(d):
    """菜品库里一道菜的必需原料。HowToCook 有的写成一串（「油，盐，生抽」「菜椒和甜椒」），拆开。"""
    out = []
    for i in (d or {}).get("ingredients", []):
        if not i.get("optional"):
            for x in re.split(r"[，,、；;/]|(?<=\S)和(?=\S)", i["name"]):
                x = re.sub(r"[`*]|^[^：:]*[：:]", "", x).strip()  # 去掉 Markdown 符号和「辅料：」
                if x and len(x) <= 10 and "。" not in x:  # 「注：如果有可能，请尽量把刀磨的锋利一些。」这种是备注
                    out.append(x)
    return list(dict.fromkeys(out))


# ---------- 买过的东西 ----------
# 渠道只是来源标记，服务端不认识任何具体平台：买菜平台的订单由各自的导入脚本整理好推进来，手输、截图让大模型识别后也走同一个接口

PURCHASE_CATS = ["蔬菜", "菌菇", "猪肉", "牛羊肉", "禽肉", "水产", "蛋奶豆制品", "水果", "主食粮油", "调料", "零食饮品", "日用"]


def purchase_row(r):
    return {"id": r["id"], "time": r["time"], "channel": r["channel"], "ref": r["ref"], "name": r["name"],
            "spec": r["spec"], "count": r["count"], "price": r["price"],
            "ingredient": r["ingredient"] or "", "cat": r["cat"] or ""}


def set_alias(name, ingredient, cat, c):
    c.execute("""INSERT INTO aliases VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET
                 ingredient = excluded.ingredient, cat = excluded.cat, updated = excluded.updated""",
              (name, ingredient.strip(), cat.strip(), now().isoformat(timespec="seconds")))


@app.post("/api/purchases")
async def add_purchases(request: Request):
    """记一批买过的东西：{channel, items: [{time, name, ref?, spec?, count?, price?, ingredient?, cat?, raw?}]}
    time 是 ISO 时间（只有日期也行）；ref 是渠道里的订单号，同一渠道同一 ref 同一商品名只记一次，重复推不会多记。
    带了 ingredient/cat 的顺便写进 aliases（截图识别、手输时一起给）。返回新增几条、跳过几条、哪些商品名还没对上原料。"""
    body = await request.json()
    channel = (body.get("channel") or "").strip()
    if not channel:
        raise HTTPException(400, "channel 不能为空（比如 manual、screenshot，或者平台名）")
    added = skipped = 0
    stamp = now().isoformat(timespec="seconds")
    with db() as c:
        for it in body.get("items") or []:
            name = (it.get("name") or "").strip()
            try:
                t = dt.datetime.fromisoformat(str(it.get("time") or stamp)).isoformat(timespec="seconds")
            except ValueError:
                raise HTTPException(400, f"时间格式不对：{it.get('time')}")
            if not name:
                continue
            cur = c.execute("""INSERT OR IGNORE INTO purchases (time, channel, ref, name, spec, count, price, raw, created)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (t, channel, str(it.get("ref") or t), name, it.get("spec") or "", float(it.get("count") or 1),
                             None if it.get("price") in (None, "") else float(it["price"]), dumps(it.get("raw") or {}), stamp))
            added += cur.rowcount
            skipped += 1 - cur.rowcount
            if it.get("ingredient") or it.get("cat"):
                set_alias(name, it.get("ingredient") or "", it.get("cat") or "", c)
        unmapped = [r[0] for r in c.execute("""SELECT DISTINCT p.name FROM purchases p LEFT JOIN aliases a ON a.name = p.name
                                               WHERE a.name IS NULL OR (a.ingredient = '' AND a.cat = '')""")]
    return {"added": added, "skipped": skipped, "unmapped": unmapped}


@app.get("/api/purchases")
def list_purchases(days: int = 90, channel: str = ""):
    since = (now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    sql = """SELECT p.*, a.ingredient, a.cat FROM purchases p LEFT JOIN aliases a ON a.name = p.name
             WHERE p.time >= ?""" + (" AND p.channel = ?" if channel else "") + " ORDER BY p.time DESC, p.id"
    with db() as c:
        rows = c.execute(sql, (since, channel) if channel else (since,)).fetchall()
    return [purchase_row(r) for r in rows]


@app.delete("/api/purchases/{pid}")
def delete_purchase(pid: int):
    with db() as c:
        if not c.execute("DELETE FROM purchases WHERE id = ?", (pid,)).rowcount:
            raise HTTPException(404)
    return {"ok": True}


@app.get("/api/aliases")
def list_aliases(unmapped: bool = False):
    """商品名 → 原料、品类。unmapped=1 只看还没对上的（买过但 aliases 里没有或空着）。"""
    with db() as c:
        rows = c.execute("""SELECT p.name, a.ingredient, a.cat, COUNT(*) AS n FROM purchases p
                            LEFT JOIN aliases a ON a.name = p.name GROUP BY p.name ORDER BY n DESC""").fetchall()
    out = [{"name": r["name"], "ingredient": r["ingredient"] or "", "cat": r["cat"] or "", "times": r["n"]} for r in rows]
    if unmapped:
        out = [x for x in out if not x["ingredient"] and not x["cat"]]
    return {"cats": PURCHASE_CATS, "aliases": out}


@app.put("/api/aliases")
async def put_aliases(request: Request):
    """批量设对应关系：{"一点小葱 约25g": {"ingredient": "小葱", "cat": "蔬菜"}, ...}。
    组合装的 ingredient 用顿号分开（「茄子、豆角」）；日用品、零食不是做菜的原料，ingredient 留空、只填 cat。"""
    body = await request.json()
    with db() as c:
        for name, v in body.items():
            set_alias(name, v.get("ingredient") or "", v.get("cat") or "", c)
    return {"ok": True, "count": len(body)}


@app.get("/api/purchases/stats")
def purchase_stats(days: int = 365):
    """按原料汇总：买过几次、最近一次哪天、平均隔几天买一次、从哪些渠道买。推荐菜和估库存用。"""
    since = (now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    with db() as c:
        rows = c.execute("""SELECT a.ingredient, a.cat, p.time, p.channel FROM purchases p JOIN aliases a ON a.name = p.name
                            WHERE p.time >= ? AND a.ingredient != '' ORDER BY p.time""", (since,)).fetchall()
    by = {}
    for r in rows:
        for ing in r["ingredient"].split("、"):  # 组合装（「茄子豆角组合」）对到几种原料，用顿号分开
            x = by.setdefault(ing, {"ingredient": ing, "cat": r["cat"], "days": [], "channels": set()})
            x["days"].append(r["time"][:10])
            x["channels"].add(r["channel"])
    out = []
    for x in by.values():
        ds = sorted(set(x["days"]))
        gaps = [(dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days for a, b in zip(ds, ds[1:])]
        out.append({"ingredient": x["ingredient"], "cat": x["cat"], "times": len(ds), "last": ds[-1],
                    "every_days": round(sum(gaps) / len(gaps), 1) if gaps else None, "channels": sorted(x["channels"])})
    out.sort(key=lambda x: -x["times"])
    return out


# ---------- 库存（估的） ----------
# 按「份」记账，不按克：买一次 = 几份 + 到期日；阿姨做饭那天每道菜用到的原料扣 1 份（鸡蛋按个），从先到期的扣；
# 到期没用完当没了。账不准靠用户在网页上改剩几份（stock_adjust），本来就是预估量（2026-11-11 用户定）。
# 菜谱和买菜的叫法不一样，用 resolve() 统一：aliases 表优先，其次 SYNONYMS，一个叫法可以对几种原料（「牛肉」→ 牛肉丝、牛腩……）

def arrive_time():
    """阿姨几点到：过了这个点，这天的菜算用掉。"""
    return hm(schedule()["arrive"])
LEAFY = {"菠菜", "生菜", "青菜", "空心菜", "菜心", "韭菜", "韭黄", "豆芽", "芹菜", "蒜苗", "秋葵"}
ROOTS = {"土豆", "红薯", "紫薯", "洋葱", "胡萝卜", "南瓜", "白萝卜", "冬瓜", "莲藕", "山药", "姜", "大蒜", "大葱", "包菜", "玉米"}
SHELF_CAT = {"蔬菜": 5, "菌菇": 4, "猪肉": 3, "牛羊肉": 3, "禽肉": 3, "水产": 2, "蛋奶豆制品": 7, "水果": 7, "主食粮油": 30,
             "调料": 365}
SHELF_ING = {"鸡蛋": 21, "皮蛋": 30, "嫩豆腐": 3, "老豆腐": 3, "酸奶": 14, "奶酪": 30, "火锅料": 30}
MEAT = {"猪肉", "牛羊肉", "禽肉"}
BIG_MEAT_G = 400  # 大份肉分装，够做 2 次、放一周；小份当天就做
MULTI_USE = {"小葱": 3, "姜": 3, "大蒜": 3, "小米辣": 3, "大葱": 3}  # 一份能用几次
EGGS_PER_DISH = 2
SYNONYMS = {
    "西红柿": "番茄", "马铃薯": "土豆", "番薯": "红薯", "地瓜": "红薯", "紫洋葱": "洋葱", "蒜苔": "蒜薹", "青蒜": "蒜苗",
    "青辣椒": "青椒", "菜椒": "青椒", "尖椒": "青椒", "甜椒": "彩椒、红椒", "红辣椒": "红椒", "小米椒": "小米辣",
    "菜花": "花菜", "花椰菜": "花菜", "圆白菜": "包菜", "卷心菜": "包菜", "包心菜": "包菜", "甘蓝": "包菜",
    "上海青": "青菜", "小青菜": "青菜", "油菜": "青菜", "西芹": "芹菜", "藕": "莲藕", "绿豆芽": "豆芽", "黄豆芽": "豆芽",
    "蘑菇": "口蘑", "鲜香菇": "香菇", "黑木耳": "木耳", "豆腐": "嫩豆腐、老豆腐", "内酯豆腐": "嫩豆腐", "南豆腐": "嫩豆腐",
    "北豆腐": "老豆腐", "黄鱼": "大黄鱼", "鸡蛋液": "鸡蛋",
    "葱": "小葱", "葱花": "小葱", "香葱": "小葱", "蒜": "大蒜", "蒜末": "大蒜", "蒜瓣": "大蒜", "蒜头": "大蒜", "蒜蓉": "大蒜",
    "生姜": "姜", "姜片": "姜", "姜末": "姜", "老姜": "姜",
    "鸡肉": "鸡肉、鸡腿、鸡腿肉、鸡胸肉", "鸡腿": "鸡腿、鸡腿肉", "鸡腿肉": "鸡腿肉、鸡腿", "鸡胸": "鸡胸肉", "鸡翅": "鸡翅中",
    "牛肉": "牛肉丝、牛肉片、牛柳、牛腩、肥牛卷", "肥牛": "肥牛卷", "牛里脊": "牛柳",
    "猪肉": "猪肉丝、里脊肉、五花肉、猪肉末", "瘦肉": "里脊肉、猪肉丝", "肉丝": "猪肉丝", "肉末": "猪肉末", "猪肉馅": "猪肉末",
    "肉馅": "猪肉末", "猪里脊": "里脊肉", "里脊": "里脊肉", "带皮五花肉": "五花肉", "肋排": "排骨", "猪排骨": "排骨", "小排": "排骨",
    "大排": "里脊肉", "通脊": "里脊肉",
}


def alias_map():
    with db() as c:
        return {r[0]: (r[1], r[2]) for r in c.execute("SELECT name, ingredient, cat FROM aliases")}


def resolve(name, al):
    """菜谱或手输的叫法 → 原料名列表。都对不上时按包含找（「去皮猪肉」→ 猪肉），调料类的词不找（「红薯淀粉」不是红薯）。"""
    ing = (al.get(name) or ("", ""))[0] or SYNONYMS.get(name)
    if not ing and not re.search(r"[粉酱油汁水料精醋糖]$", name):
        keys = set(SYNONYMS) | {i for v, _ in al.values() for i in v.split("、") if i}
        hit = max((k for k in keys if len(k) >= 2 and k in name), key=len, default="")
        ing = SYNONYMS.get(hit) or hit
    return [x for x in (ing or name).split("、") if x]


def pantry_like(name, al):
    """常备调料：PANTRY 里的，或者买过、品类是调料的。"""
    return name in PANTRY or any(cat_of(i, al) == "调料" for i in resolve(name, al))


def cat_of(ing, al):
    return next((c for i, c in al.values() if c and ing in i.split("、")), "")


def shelf_days(ing, cat, grams):
    if cat in MEAT:
        return 7 if grams >= BIG_MEAT_G else 3
    if ing in SHELF_ING:
        return SHELF_ING[ing]
    if cat == "蔬菜":
        return 3 if ing in LEAFY else 14 if ing in ROOTS else 5
    return SHELF_CAT.get(cat, 7)


def pack_uses(ing, cat, grams, text):
    """一份能用几次（鸡蛋返回个数）。"""
    if ing == "鸡蛋":
        m = re.search(r"(\d+)\s*[枚个]", text)
        return int(m.group(1)) if m else 10
    if cat in MEAT and grams >= BIG_MEAT_G:
        return 2
    return MULTI_USE.get(ing, 1)


def grams_of(spec):
    m = re.match(r"([\d.]+)\s*(kg|g|ml|mL|l|L)?", spec or "")
    if not m:
        return 0
    return float(m.group(1)) * (1000 if (m.group(2) or "").lower() in ("kg", "l") else 1)


def estimate_stock(at=None, since_days=60):
    """把买、用、校正按时间顺序过一遍，返回 at 时刻每种原料的批次（剩几次、哪天到期）。"""
    at = at or now()
    al = alias_map()
    since = (at - dt.timedelta(days=since_days)).isoformat(timespec="seconds")
    events = []  # (时间, 顺序, 类型, 数据)：同一时刻先买、再校正、再用
    with db() as c:
        for r in c.execute("SELECT * FROM purchases WHERE time >= ? AND time <= ?", (since, at.isoformat())):
            events.append((dt.datetime.fromisoformat(r["time"]), 0, "buy", r))
        for r in c.execute("SELECT * FROM stock_adjust WHERE time >= ? AND time <= ?", (since, at.isoformat())):
            events.append((dt.datetime.fromisoformat(r["time"]), 1, "set", r))
    lib = {d["name"]: d for d in library()}
    arrive, workdays = arrive_time(), schedule()["workdays"]
    for m in all_menus():
        t = dt.datetime.combine(dt.date.fromisoformat(m["date"]), arrive)
        if since <= t.isoformat() <= at.isoformat() and m.get("mode", "cook") == "cook" and t.weekday() in workdays:
            names = [x for d in m.get("dishes", []) for x in dish_ingredients(lib.get(d))] + m.get("steam", [])
            events.append((t, 2, "use", names))
    events.sort(key=lambda e: (e[0], e[1]))
    batches = {}  # 原料 → [{left, per, bought, expires}]

    def alive(ing, t):
        bs = [b for b in batches.get(ing, []) if b["left"] > 0 and b["expires"] >= t.date()]
        return sorted(bs, key=lambda b: b["expires"])

    for t, _, kind, x in events:
        if kind == "buy":
            ing_cat = al.get(x["name"]) or ("", "")
            ings = [i for i in ing_cat[0].split("、") if i]
            g = grams_of(x["spec"])
            for ing in ings:
                cat = ing_cat[1]
                per = pack_uses(ing, cat, g, x["name"] + " " + x["spec"])
                batches.setdefault(ing, []).append({"left": x["count"] * per, "per": per, "bought": t.date(), "cat": cat,
                                                    "expires": t.date() + dt.timedelta(days=shelf_days(ing, cat, g))})
        elif kind == "set":
            ing, left = x["ingredient"], x["amount"]
            old = alive(ing, t)
            batches[ing] = []
            if left > 0:
                cat = old[0]["cat"] if old else cat_of(ing, al)
                exp = max((b["expires"] for b in old), default=t.date() + dt.timedelta(days=shelf_days(ing, cat, 0)))
                batches[ing].append({"left": left, "per": old[0]["per"] if old else 1, "bought": t.date(),
                                     "cat": cat, "expires": exp})
        else:
            for name in dict.fromkeys(x):
                for ing in resolve(name, al):
                    bs = alive(ing, t)
                    if bs and bs[0]["cat"] != "调料":  # 调料一瓶用很久，不按菜扣
                        need = (EGGS_PER_DISH if ing == "鸡蛋" else 1) * x.count(name)
                        for b in bs:
                            use = min(need, b["left"])
                            b["left"] -= use
                            need -= use
                            if need <= 0:
                                break
                        break
    left = {ing: sum(b["left"] for b in alive(ing, at)) for ing in batches}
    return {"batches": batches, "left": left}


def stock_unit(ing, per):
    return "个" if ing == "鸡蛋" else "份" if per == 1 else "次"


@app.get("/api/stock")
def get_stock():
    """家里有什么（估的）：每种原料剩几份、哪天到期；过期 3 天内的也列出来（可能坏了）；原料都齐、现在就能做的菜。"""
    t = now()
    st = estimate_stock(t)
    al = alias_map()
    items = []
    for ing, bs in st["batches"].items():
        live = [b for b in bs if b["left"] > 0 and b["expires"] >= t.date()]
        gone = [b for b in bs if b["left"] > 0 and dt.timedelta(0) < t.date() - b["expires"] <= dt.timedelta(days=3)]
        for group, expired in ((live, False), (gone, True)):
            if not group:
                continue
            per = group[0]["per"]
            n = sum(b["left"] for b in group)
            exp = min(b["expires"] for b in group)
            items.append({"ingredient": ing, "cat": group[0]["cat"], "left": round(n, 1), "unit": stock_unit(ing, per),
                          "bought": max(b["bought"] for b in group).isoformat(), "expires": exp.isoformat(),
                          "days_left": (exp - t.date()).days, "expired": expired})
    items.sort(key=lambda x: (x["expired"] is False, x["days_left"]))
    have = {x["ingredient"] for x in items if not x["expired"]}
    stats, _ = usage()
    can = []
    for d in library():
        if d["hidden"]:
            continue
        got, miss = dish_stock(d, have, al)
        if got and not miss:
            can.append((d["name"], stats["dishes"][d["name"]], len(got)))
    can.sort(key=lambda x: (-x[1], -x[2]))
    return {"items": items, "can_cook": [x[0] for x in can[:30]], "cats": PURCHASE_CATS}


def dish_stock(d, have, al):
    """一道菜调料以外的原料，哪些家里有、哪些缺。没买过的叫法（西葫芦、量杯）算缺。"""
    need = [n for n in dish_ingredients(d) if not pantry_like(n, al)]
    got = [n for n in need if set(resolve(n, al)) & have]
    return got, [n for n in need if n not in got]


@app.get("/api/stock/dishes")
def stock_dishes(date: str = ""):
    """选菜页用：按这天阿姨来之前的库存，列出至少有一样主料、最多缺一样的菜。
    soon = 用到了明天前到期的原料（先把快坏的用掉）。"""
    day = check_date(date) if date else now().date()
    at = dt.datetime.combine(day, arrive_time()) - dt.timedelta(minutes=1)
    st, al = estimate_stock(at), alias_map()
    live = [(ing, b) for ing, bs in st["batches"].items() for b in bs if b["left"] > 0 and b["expires"] >= day]
    have = {ing for ing, _ in live}
    soon = {ing for ing, b in live if b["expires"] <= day + dt.timedelta(days=1) and b["cat"] != "调料"}
    out = []
    for d in library():
        if d["hidden"]:
            continue
        got, miss = dish_stock(d, have, al)
        if got and len(miss) <= 1:
            out.append({"name": d["name"], "missing": miss,
                        "soon": any(set(resolve(n, al)) & soon for n in got)})
    return {"date": day.isoformat(), "dishes": out}


@app.post("/api/stock")
async def adjust_stock(request: Request):
    """校正：{ingredient, left, reason?}。left 是剩几份（鸡蛋是个数），0 = 用完了 / 扔了（reason: used / thrown / fix）。"""
    body = await request.json()
    ing = (body.get("ingredient") or "").strip()
    if not ing:
        raise HTTPException(400, "ingredient 不能为空")
    with db() as c:
        c.execute("INSERT INTO stock_adjust (time, ingredient, amount, reason) VALUES (?, ?, ?, ?)",
                  (now().isoformat(timespec="milliseconds"), ing, max(0.0, float(body.get("left") or 0)), body.get("reason") or "fix"))
    return get_stock()


@app.post("/api/stock/add")
async def add_stock(request: Request):
    """网页上「记一笔」：{text: "土豆 3个、鸡蛋 10个"}，记成 manual 渠道的购买。认识的叫法自动对上原料。"""
    text = (await request.json()).get("text") or ""
    al, items, stamp = alias_map(), [], now().isoformat(timespec="milliseconds")  # 精确到毫秒，免得和刚做的校正同一秒、排序颠倒
    for part in re.split(r"[、，,；;\n]+", text):
        m = re.match(r"^\s*(.+?)\s*(\d+(?:\.\d+)?)?\s*(个|枚|份|包|盒|袋|根|颗|把|条|只|斤|g|克|kg)?\s*$", part)
        if not m or not m.group(1):
            continue
        name, n, unit = m.group(1), float(m.group(2) or 1), m.group(3) or ""
        ings = resolve(name, al)
        egg = ings == ["鸡蛋"] and unit in ("个", "枚", "")
        items.append({"time": stamp, "name": name, "count": 1 if egg else n, "spec": f"{n:g}个" if egg else unit,
                      "ingredient": "、".join(ings), "cat": next((cat_of(i, al) for i in ings if cat_of(i, al)), "")})
    if not items:
        raise HTTPException(400, "没看懂，写成「土豆 3个、鸡蛋 10个」这样")
    with db() as c:
        for it in items:
            c.execute("""INSERT OR IGNORE INTO purchases (time, channel, ref, name, spec, count, raw, created)
                         VALUES (?, 'manual', ?, ?, ?, ?, '{}', ?)""", (it["time"], it["time"], it["name"], it["spec"], it["count"], stamp))
            if it["name"] not in al:
                set_alias(it["name"], it["ingredient"], it["cat"], c)
    return {"added": [x["name"] for x in items],
            "unknown": [x["name"] for x in items if not x["cat"]], **get_stock()}


# ---------- 电量 ----------

def bat_raw_level(mv):
    return sum(mv >= s for s in BAT_STEPS)


def bat_level(mv, prev):
    """带回差的格数：掉格要明显低于阈值，涨格要明显高于（一般是充过电），免得在边界上来回刷屏。"""
    if not mv:
        return prev
    if mv >= CHARGING_MV:
        return len(BAT_STEPS)
    raw = bat_raw_level(mv)
    if prev is None:
        return raw
    if raw < prev and bat_raw_level(mv + 30) < prev:
        return raw
    if raw > prev and bat_raw_level(mv - 60) > prev:
        return raw
    return prev


# WiFi 信号分 1～4 格（按 RSSI），同样带回差
RSSI_STEPS = [-82, -74, -67, -60]


def wifi_level(rssi, prev):
    if not rssi:
        return prev
    lv = lambda r: sum(r >= s for s in RSSI_STEPS)
    raw = lv(rssi)
    if prev is None:
        return raw
    if raw < prev and lv(rssi + 4) < prev:
        return raw
    if raw > prev and lv(rssi - 4) > prev:
        return raw
    return prev


def log_battery(mv, reason):
    if not mv:
        return
    with db() as c:
        c.execute("INSERT INTO battery (time, mv, reason) VALUES (?, ?, ?)", (now().isoformat(timespec="seconds"), mv, reason))


def bat_estimate():
    """用最近一次充电之后的读数拟合每天掉多少 mV，估算还能用几天。数据不够返回 None。"""
    with db() as c:
        rows = [(dt.datetime.fromisoformat(t), mv) for t, mv in c.execute("SELECT time, mv FROM battery ORDER BY id")]
    # 只看最近一次充电之后
    for i in range(len(rows) - 1, -1, -1):
        if rows[i][1] >= CHARGING_MV:
            rows = rows[i + 1:]
            break
    if len(rows) < 3 or (rows[-1][0] - rows[0][0]).total_seconds() < 86400:
        return None
    t0 = rows[0][0]
    xs = [(t - t0).total_seconds() / 86400 for t, _ in rows]
    ys = [mv for _, mv in rows]
    n, sx, sy = len(xs), sum(xs), sum(ys)
    sxx, sxy = sum(x * x for x in xs), sum(x * y for x, y in zip(xs, ys))
    if n * sxx - sx * sx == 0:
        return None
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)  # mV/天，负数
    if slope >= -1:
        return {"mv_per_day": round(-slope, 1), "days_left": None}
    return {"mv_per_day": round(-slope, 1), "days_left": max(0, round((ys[-1] - BAT_STEPS[0]) / -slope))}


# ---------- 画图 ----------

def wrap(text, f, width):
    lines, cur = [], ""
    for ch in text:
        if f.getlength(cur + ch) > width and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def show_day(t=None):
    """屏幕现在该显示哪天：今天阿姨来且没过切换时间，就是今天；否则是她下一次来的日子（要已经录入，
    不然还显示今天）。周六晚上直接显示周一。"""
    t = t or now()
    today = t.date()
    if day_mode(today) != "off" and t.time() < hm(settings()["switch_time"]):
        return today
    nxt = next_visit(today)
    return nxt if load_menu(nxt) else today


def fit(d, items, x, y, width, bottom, sizes, prefix=lambda i: ""):
    """从大到小试字号：优先选每条都不换行、整列放得下的；都不行再选放得下的（会换行）。返回画完后的 y。"""
    def layout(size):
        f = font(size)
        lines = []
        for i, s in enumerate(items):
            for j, line in enumerate(wrap(prefix(i) + s, f, width)):
                lines.append(line if j == 0 else "　" + line)
        return f, int(size * 1.32), lines

    choice = None
    for need_single in (True, False):
        for size in sizes:
            f, gap, lines = layout(size)
            if y + len(lines) * gap <= bottom and (not need_single or len(lines) == len(items)):
                choice = (f, gap, lines)
                break
        if choice:
            break
    f, gap, lines = choice or layout(sizes[-1])
    for line in lines:
        if y + gap > bottom + 6:
            break
        d.text((x, y), line, font=f, fill=BLACK)
        y += gap
    return y


def battery_icon(d, x, y, level):
    """标题栏里的电池图标（红底白线），4 格；level 为 None 不画。"""
    if level is None:
        return
    w, h = 46, 24
    d.rectangle([x, y, x + w, y + h], outline=WHITE, width=3)
    d.rectangle([x + w + 1, y + 7, x + w + 5, y + h - 7], fill=WHITE)
    for i in range(level):
        bx = x + 6 + i * 9
        d.rectangle([bx, y + 6, bx + 6, y + h - 6], fill=WHITE)


def wifi_icon(d, x, y, level):
    """标题栏里的 WiFi 信号格（红底白色竖条，4 格，没亮的画空心）；level 为 None 不画。"""
    if level is None:
        return
    for i in range(4):
        bx, bh = x + i * 9, 8 + i * 6
        box = [bx, y + 26 - bh, bx + 6, y + 26]
        if i < level:
            d.rectangle(box, fill=WHITE)
        else:
            d.rectangle(box, outline=WHITE, width=1)


def render(day: dt.date, level=None, wifi=None):
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # 墨水屏只有纯色，关掉抗锯齿
    today = now().date()
    label = {0: "今日菜单", 1: "明日菜单", -1: "昨日菜单"}.get((day - today).days, "菜单")

    # 顶部红条：日期、电池、今日/明日
    d.rectangle([0, 0, W, 66], fill=RED)
    d.text((24, 33), f"{day.month}月{day.day}日  星期{WEEK[day.weekday()]}", font=font(38), fill=WHITE, anchor="lm")
    d.text((W - 24, 33), label, font=font(38), fill=WHITE, anchor="rm")
    bx = W - 24 - int(font(38).getlength(label)) - 76
    battery_icon(d, bx, 21, level)
    wifi_icon(d, bx - 50, 20, wifi)

    m = load_menu(day) or {}
    mode = day_mode(day)
    dishes, steam, tasks, note = m.get("dishes", []), m.get("steam", []), m.get("tasks", []), m.get("note", "")
    tasks = tasks + [x["name"] for x in chores_on(day.isoformat()) if x["name"] not in tasks]  # 周期任务和留言一起显示
    top, bottom, split = 80, H - 12, 500
    d.line([split, top, split, bottom], fill=BLACK, width=2)

    # 左栏：菜，下面是蒸的东西，最下面备注
    lx, lw = 24, split - 48
    y_end = bottom
    if note:
        nf = font(26, bold=False)
        nl = wrap("备注：" + note, nf, lw)[:2]
        y_end = bottom - 34 * len(nl)
        for i, line in enumerate(nl):
            d.text((lx, y_end + 34 * i), line, font=nf, fill=BLACK)
        y_end -= 8
    if steam and mode == "cook":
        sf = font(36)
        sl = wrap("、".join(steam), sf, lw - 64)
        sy = y_end - (14 + len(sl[:2]) * 46)
        d.line([lx, sy, split - 24, sy], fill=BLACK, width=2)
        d.text((lx, sy + 12), "蒸", font=font(36), fill=RED)
        for i, line in enumerate(sl[:2]):
            d.text((lx + 64, sy + 12 + 46 * i), line, font=sf, fill=BLACK)
        y_end = sy - 6
    if mode != "cook":  # 来了不做饭、不来的日子左栏不放菜，大字写明
        big = "今天不用做饭" if mode == "cat" else "阿姨不来"
        d.text((lx + lw // 2, (top + y_end) // 2), big, font=font(56), fill=RED, anchor="mm")
    elif dishes:
        d.text((lx, top), "菜", font=font(32), fill=RED)
        fit(d, dishes, lx, top + 44, lw, y_end, (44, 40, 36, 32, 28), lambda i: f"{i + 1}. ")
    else:
        d.text((lx, top), "菜", font=font(32), fill=RED)
        d.text((lx, top + 50), "还没有菜单", font=font(40), fill=BLACK)

    # 右栏：留言
    rx, rw = split + 22, W - split - 40
    d.text((rx, top), "留言", font=font(32), fill=RED)
    low = level == 0
    tasks_bottom = bottom - (78 if low else 34)
    if tasks:
        fit(d, tasks, rx, top + 44, rw, tasks_bottom, (36, 32, 28, 26, 24))
    else:
        d.text((rx, top + 50), "今天没有", font=font(30, bold=False), fill=BLACK)

    # 右下角：更新时间；电量低时加一行红字
    small = font(20, bold=False)
    if m.get("updated"):
        u = dt.datetime.fromisoformat(m["updated"])
        d.text((W - 16, bottom), f"更新于 {u.month}月{u.day}日 {u:%H:%M}", font=small, fill=BLACK, anchor="rs")
    if low:
        d.text((W - 16, bottom - 30), "电量低，请充电", font=font(30), fill=RED, anchor="rs")
    return img


def planes(img):
    """转成 GxEPD2 的两层位图：黑层、红层，0 表示上色。按 BAND 行一段交替排列。"""
    px = img.load()
    black = bytearray(b"\xff" * (W * H // 8))
    red = bytearray(b"\xff" * (W * H // 8))
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            i, bit = (y * W + x) >> 3, 0x80 >> (x & 7)
            if r > 128 and g < 128:
                red[i] &= ~bit
            elif r < 128:
                black[i] &= ~bit
    row = W // 8
    out = bytearray()
    for y in range(0, H, BAND):
        out += black[y * row:(y + BAND) * row]
        out += red[y * row:(y + BAND) * row]
    return bytes(out)


def png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


def log_event(ev):
    ev = dict(ev)
    with db() as c:
        c.execute("INSERT INTO events (time, type, data) VALUES (?, ?, ?)",
                  (now().isoformat(timespec="seconds"), ev.pop("type"), dumps(ev)))
        c.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 3000")  # 只留最近 3000 条


def device_state():
    return kv_get("device", {})


@app.get("/preview.png")
def preview(date: str = ""):
    day = check_date(date) if date else show_day()
    st = device_state()
    return png(render(day, st.get("bat_level"), st.get("wifi_level")))


# ---------- 屏幕 ----------

def next_sleep(t=None):
    # 排查用：数据目录下的 sleep_override 里写秒数，屏幕就按这个睡（测定时唤醒），删掉恢复
    ov = DATA / "sleep_override"
    if ov.exists():
        return int(ov.read_text().strip() or 60)
    t = t or now()
    for days in range(8):
        for s in wake_slots(t.date() + dt.timedelta(days=days)):
            w = dt.datetime.combine(t.date() + dt.timedelta(days=days), hm(s))
            if w > t + dt.timedelta(seconds=60):
                return max(60, int((w - now()).total_seconds()))
    return 3600


def snap_to_wake(t=None):
    """屏幕的 RTC 闹钟不准，睡几个小时可能早醒几分钟。在某个醒来时间前 15 分钟内来的，按那个时间算，
    免得切换时间和醒来时间设成同一个点时，早醒一点就错过切换。"""
    t = t or now()
    for s in wake_slots(t.date()):
        w = dt.datetime.combine(t.date(), hm(s))
        if dt.timedelta(0) < w - t <= dt.timedelta(minutes=15):
            return w
    return t


def fw_version():
    p = FW_DIR / "version.txt"
    return p.read_text().strip() if p.exists() else ""


@app.get("/api/frame")
def frame(request: Request, bat: int = 0, rssi: int = 0, t: float = -1000, h: float = -1, fw: str = "",
          reason: str = "", diag: str = ""):
    prev = device_state()
    eff = snap_to_wake()  # 早醒几分钟的，按准点算
    sleep = next_sleep(eff)
    level = bat_level(bat, prev.get("bat_level"))
    wifi = wifi_level(rssi, prev.get("wifi_level"))
    state = {"seen": now().isoformat(timespec="seconds"), "bat": bat or None, "bat_level": level, "wifi_level": wifi,
             "charging": bool(bat and bat >= CHARGING_MV), "rssi": rssi or None,
             "temp": t if t > -100 else prev.get("temp"), "hum": h if h >= 0 else prev.get("hum"),
             "fw": fw, "reason": reason, "diag": diag or None,
             "ip": request.headers.get("x-real-ip") or (request.client.host if request.client else None),
             "next_wake": (now() + dt.timedelta(seconds=sleep + 30)).replace(second=0).isoformat(timespec="seconds")}
    kv_set("device", state)
    log_battery(bat, reason)
    emit("device_seen", "屏幕醒来", f"电量 {level} 格，信号 {wifi} 格",
         {k: state[k] for k in ("bat", "bat_level", "charging", "rssi", "wifi_level", "temp", "hum", "fw", "reason")})
    if level == 0 and not state["charging"] and prev.get("bat_level") != 0:
        emit("battery_low", "屏幕该充电了", f"电池 {bat} mV，到 0 格了，找时间充一下电", {"bat": bat})

    data = planes(render(show_day(eff), level, wifi))
    etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
    headers = {"ETag": etag, "X-Sleep": str(sleep), "X-Fw": fw_version(), "X-Band": str(BAND)}
    same = request.headers.get("if-none-match") == etag
    upd = headers["X-Fw"] if headers["X-Fw"] and headers["X-Fw"] != fw else None
    log_event({"type": "wake", "reason": reason, "bat": bat or None, "rssi": rssi or None, "fw": fw,
               "drawn": not same, "showing": show_day(eff).isoformat(), "sleep": sleep, "update_to": upd})
    if same:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type="application/octet-stream", headers=headers)


@app.get("/api/telemetry")
def telemetry(t: float = -1000, h: float = -1, diag: str = ""):
    """屏幕刷完图后补报的温湿度。"""
    state = device_state()
    if t > -100:
        state.update(temp=t, hum=h if h >= 0 else None)
    if diag:
        state["diag"] = diag
    kv_set("device", state)
    log_event({"type": "telemetry", "temp": t if t > -100 else None, "hum": h if h >= 0 else None, "diag": diag or None})
    return {"ok": True}


@app.get("/api/logs")
def logs(kind: str = "device", limit: int = 200):
    """日志页：device = 屏幕每次请求，edits = 每次保存菜单。新的在前。"""
    with db() as c:
        if kind == "edits":
            return [dict(r) for r in c.execute("SELECT time, menu_date, client, summary FROM edits ORDER BY id DESC LIMIT ?",
                                               (limit,))]
        rows = c.execute("SELECT time, type, data FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"time": t, "type": ty, **json.loads(d)} for t, ty, d in rows]


@app.get("/api/device")
def device():
    return {**device_state(), "fw_latest": fw_version(), "wake_times": wake_slots(),
            "showing": show_day().isoformat(), "battery": bat_estimate(), "bat_steps": BAT_STEPS}


def ts(x):
    return dt.datetime.fromisoformat(x).timestamp() if x else None


@app.get("/metrics")
def metrics():
    """Prometheus 文本格式，给 Prometheus / VictoriaMetrics 抓，想在 Grafana 里做告警时用（不用的话通知在设置里配）。"""
    st, t = device_state(), now()
    est = bat_estimate() or {}
    visit = visit_day(t)  # 下一次阿姨上门
    vm = load_menu(visit) or empty_menu(visit)
    lw = kv_get("laundry") or {}
    at10 = lambda d: dt.datetime.combine(dt.date.fromisoformat(d) if isinstance(d, str) else d, arrive_time()).timestamp()
    rows = [
        ("menu_device_last_seen_timestamp_seconds", "屏幕上次联网", ts(st.get("seen"))),
        ("menu_device_next_wake_timestamp_seconds", "服务器让屏幕下次几点醒", ts(st.get("next_wake"))),
        ("menu_device_battery_mv", "上次醒来的电池电压", st.get("bat")),
        ("menu_device_battery_level", "电量格数 0～4，0 = 该充电了", st.get("bat_level")),
        ("menu_device_battery_days_left", "按放电速度估算还能用几天", est.get("days_left")),
        ("menu_device_charging", "是否插着充电", int(bool(st.get("charging")))),
        ("menu_device_rssi_dbm", "上次醒来的 WiFi 信号", st.get("rssi")),
        ("menu_device_firmware_outdated", "屏幕固件比服务器上发布的旧", int(bool(st.get("fw")) and st.get("fw") != fw_version())),
        ("menu_visit_timestamp_seconds", "下一次阿姨上门那天她到的时间", at10(visit)),
        ("menu_visit_cook", "那天是做饭（不是来了不做饭）", int(day_mode(visit) == "cook")),
        ("menu_visit_dishes", "那天录了几道菜", len(vm["dishes"])),
        ("menu_laundry_pending", "洗衣机洗完了、晒衣服留言还没确认也没忽略", int(lw.get("state") == "pending")),
        ("menu_laundry_target_timestamp_seconds", "洗衣机提醒的那天阿姨到的时间", at10(lw["date"]) if lw.get("date") else None),
    ]
    out = []
    for name, help_, v in rows:
        if v is not None:
            out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge", f"{name} {v}"]
    return Response("\n".join(out) + "\n", media_type="text/plain; version=0.0.4")


@app.put("/api/firmware")
async def upload_firmware(request: Request):
    """firmware/publish.sh 用：body 是 firmware.bin，header X-Fw-Version 是版本号。屏幕下次醒来看到版本不同就升级。"""
    if ADMIN_TOKEN and request.headers.get("authorization") != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "要带 Authorization: Bearer <MENU_ADMIN_TOKEN>")
    ver = (request.headers.get("x-fw-version") or "").strip()
    data = await request.body()
    if not ver or len(data) < 1024 or data[0] != 0xE9:  # ESP32 镜像第一个字节是 0xE9
        raise HTTPException(400, "要带 X-Fw-Version，body 是 firmware.bin")
    tmp = FW_DIR / "firmware.bin.tmp"
    tmp.write_bytes(data)
    tmp.replace(FW_DIR / "firmware.bin")
    (FW_DIR / "version.txt").write_text(ver + "\n")
    log_event({"type": "firmware_published", "version": ver, "size": len(data)})
    return {"ok": True, "version": ver, "size": len(data)}


@app.get("/firmware.bin")
def firmware():
    p = FW_DIR / "firmware.bin"
    if not p.exists():
        raise HTTPException(404)
    log_event({"type": "firmware", "version": fw_version()})
    return FileResponse(p, media_type="application/octet-stream")


@app.get("/", response_class=HTMLResponse)
def index():
    return Response((Path(__file__).parent / "index.html").read_text(), media_type="text/html",
                    headers={"Cache-Control": "no-cache"})


init_db()
