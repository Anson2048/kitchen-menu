"""把 HowToCook（github.com/Anduin2017/HowToCook，Unlicense）导成菜品库。

服务第一次启动、数据目录里还没有菜品库时会自动跑一次，一般不用手动跑。想更新菜品库时：
    docker compose exec menu python tools/import_howtocook.py /data/library
    docker compose restart menu
输出 <目录>/howtocook.json 和 <目录>/img/<id>.jpg（400px 缩略图）。网页上改过的菜不会被覆盖。
访问 GitHub 慢的话，用 HOWTOCOOK_RAW 换成镜像（默认 https://raw.githubusercontent.com/Anduin2017/HowToCook/master/）。
"""
import concurrent.futures as cf
import hashlib
import io
import json
import re
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

REPO = "Anduin2017/HowToCook"
RAW = os.environ.get("HOWTOCOOK_RAW", f"https://raw.githubusercontent.com/{REPO}/master/")
API = os.environ.get("GITHUB_API", "https://api.github.com")
CATS = {
    "meat_dish": "荤菜", "vegetable_dish": "素菜", "aquatic": "水产", "soup": "汤粥",
    "staple": "主食", "breakfast": "早餐", "dessert": "甜品", "drink": "饮品",
    "condiment": "调料", "semi-finished": "半成品",
}
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")

out = Path(sys.argv[1])
(out / "img").mkdir(parents=True, exist_ok=True)


def get(path):
    url = RAW + urllib.parse.quote(path)
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def section(md, title):
    m = re.search(rf"^##\s*{title}.*?$(.*?)(?=^##\s|\Z)", md, re.M | re.S)
    return m.group(1) if m else ""


def bullets(text):
    return [re.sub(r"^\s*[*\-]\s*", "", l).strip() for l in text.splitlines() if re.match(r"^\s*[*\-]\s+\S", l)]


with urllib.request.urlopen(f"{API}/repos/{REPO}/git/trees/master?recursive=1", timeout=60) as r:
    tree = json.loads(r.read())["tree"]
paths = [t["path"] for t in tree if t["type"] == "blob"]
mds = [p for p in paths if p.startswith("dishes/") and p.endswith(".md") and "/template/" not in p]


def one(md_path):
    parts = md_path.split("/")
    cat = CATS.get(parts[1])
    if not cat:
        return None
    name = Path(md_path).stem
    md = get(md_path).decode("utf-8", "replace")
    title = re.search(r"^#\s*(.+?)(的做法)?\s*$", md, re.M)
    if title:
        name = title.group(1).strip()
    desc = ""
    for para in md.split("\n\n")[1:]:
        p = para.strip()
        if p and not p.startswith(("#", "!", "预估", "*", "-")):
            desc = re.sub(r"\s+", " ", p)[:120]
            break
    stars = re.search(r"预估烹饪难度：\s*(★+)", md)
    ingredients = []
    for b in bullets(section(md, "必备原料和工具")):
        optional = "可选" in b
        n = re.sub(r"[（(].*?[)）]", "", b).strip()
        if n:
            ingredients.append({"name": n, "optional": optional})
    qty = bullets(section(md, "计算"))

    # 图：先找正文里引用的，再找同目录的第一张
    folder = "/".join(parts[:-1])
    img_path = None
    for ref in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", md):
        ref = urllib.parse.unquote(ref).lstrip("./")
        cand = f"{folder}/{ref}"
        if cand in paths and cand.lower().endswith(IMG_EXT):
            img_path = cand
            break
    if not img_path and len(parts) == 4:
        imgs = sorted(p for p in paths if p.startswith(folder + "/") and p.lower().endswith(IMG_EXT))
        img_path = imgs[0] if imgs else None

    did = hashlib.sha1(md_path.encode()).hexdigest()[:10]
    img = None
    if img_path:
        try:
            im = Image.open(io.BytesIO(get(img_path))).convert("RGB")
            im.thumbnail((400, 400))
            im.save(out / "img" / f"{did}.jpg", "JPEG", quality=72, optimize=True)
            img = f"{did}.jpg"
        except Exception as e:
            print("图片失败", img_path, e, file=sys.stderr)
    return {"id": did, "name": name, "cat": cat, "img": img, "desc": desc,
            "difficulty": len(stars.group(1)) if stars else None,
            "ingredients": ingredients, "qty": qty, "source": "howtocook", "path": md_path}


with cf.ThreadPoolExecutor(12) as ex:
    dishes = [d for d in ex.map(one, mds) if d]
dishes.sort(key=lambda d: (list(CATS.values()).index(d["cat"]), d["name"]))
(out / "howtocook.json").write_text(json.dumps(dishes, ensure_ascii=False, indent=1))
print(f"{len(dishes)} 道菜，{sum(1 for d in dishes if d['img'])} 道有图")
