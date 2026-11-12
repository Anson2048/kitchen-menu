"""生成固件里「未联网」提示的点阵（红层，0 = 红），输出 C 头文件到 stdout。
改了文字或位置才需要重新生成。在服务端容器里跑（里面有 Noto CJK 字体）：
    docker compose exec -T menu python3 - < tools/gen_badge.py > firmware/src/badge.h
"""
from PIL import Image, ImageDraw, ImageFont

W, H = 256, 44  # 放在右下角 x=536、y=432（x 要 8 的倍数）
f = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 32, index=2)
im = Image.new("1", (W, H), 1)
d = ImageDraw.Draw(im)
d.fontmode = "1"
d.text((W - 8, H // 2), "未联网", font=f, fill=0, anchor="rm")
data = im.tobytes()  # 1 位、每行 W/8 字节、高位在前，和 GxEPD2 的格式一致
print("#pragma once\n// tools/gen_badge.py 生成，别手改")
print(f"#define BADGE_X 536\n#define BADGE_Y 432\n#define BADGE_W {W}\n#define BADGE_H {H}")
print("static const uint8_t badgeRed[] PROGMEM = {")
for i in range(0, len(data), 16):
    print("  " + ", ".join(f"0x{b:02x}" for b in data[i:i + 16]) + ",")
print("};")
