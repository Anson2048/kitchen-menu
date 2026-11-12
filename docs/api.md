# 接口

全部是 JSON，日期格式 `YYYY-MM-DD`。没有认证（见 [deploy.md](deploy.md#安全)）。脚本、自动化、大模型 agent 都可以直接调。

```bash
M=http://<服务器>:8000
```

## 菜单

```bash
curl -s $M/api/menu/2026-11-12                       # 读一天
curl -s -X PUT $M/api/menu/2026-11-12 -H 'content-type: application/json' \
  -d '{"mode":"cook","dishes":["红烧肉","清炒菜心","番茄蛋汤"],"steam":["南瓜","鸡蛋"],"tasks":["晒被子"],"note":"3人份"}'
curl -s -X DELETE $M/api/menu/2026-11-12             # 删一天
curl -s "$M/api/menus?days=30"                       # 最近的菜单（推荐菜可以拿这个当历史）
curl -s -X POST $M/api/menu/2026-11-12/tasks -H 'content-type: application/json' -d '{"text":"晒衣服","auto":true}'  # 追加一条留言
curl -s -X POST $M/api/hooks/task -H 'content-type: application/json' -d '{"text":"收快递"}'   # 同上，不给日期自动算阿姨最近来的那天
open "$M/preview.png?date=2026-11-12"                # 屏幕上会是什么样
```

- `mode`：`cook` 做饭、`cat` 来了不做饭（只看留言）、`off` 不来。设置里不来的星期几固定是 `off`
- 菜名尽量和菜品库对上，原料、库存、推荐都靠它
- 读菜单时还会带 `suggest`（洗衣机建议）和 `chores`（这天排到的周期任务）

## 菜品库和常用项

```bash
curl -s $M/api/dishes                                # 菜品库：分类、图、原料、常吃次数、是否隐藏
curl -s $M/api/dishes/<id>
curl -s -X POST $M/api/dishes -H 'content-type: application/json' -d '{"name":"糖醋排骨","cat":"荤菜","ingredients":"排骨、白糖、醋"}'
curl -s -X PUT $M/api/dishes/<id> -H 'content-type: application/json' -d '{"ingredients":["排骨","冰糖"]}'
curl -s -X PUT $M/api/dishes/<id>/photo -H 'content-type: application/json' -d '{"photo":"data:image/jpeg;base64,..."}'
curl -s -X DELETE $M/api/dishes/<id>                 # 只能删自己加的；自带的用 PUT {"hidden":true} 隐藏
curl -s $M/api/presets                               # 蒸、留言、备注的常用项
```

## 设置

```bash
curl -s $M/api/settings
curl -s -X PUT $M/api/settings -H 'content-type: application/json' -d '{"wake_times":["06:00","09:30"],"switch_time":"18:00"}'
curl -s -X PUT $M/api/settings -H 'content-type: application/json' -d '{"schedule":{"workdays":[0,1,2,3,4,5],"arrive":"10:00","leave":"12:00"}}'
curl -s -X PUT $M/api/settings -H 'content-type: application/json' -d '{"notify":{"kind":"ntfy","url":"https://ntfy.sh/my-kitchen"}}'
curl -s -X PUT $M/api/settings -H 'content-type: application/json' -d '{"webhook":{"url":"http://ha:8123/api/webhook/kitchen_menu","events":["battery_low"]}}'
curl -s -X POST $M/api/notify/test -d '{}'           # 发一条测试通知（body 里可以带还没保存的配置）
curl -s -X POST $M/api/webhook/test -d '{}'
```

`PUT` 只传要改的字段。`workdays` 里 0 是周一、6 是周日。

## 周期任务

```bash
curl -s $M/api/chores                                # 带 next：排到哪天
curl -s -X POST $M/api/chores -H 'content-type: application/json' -d '{"name":"擦楼梯","every":2,"unit":"w","last":"2026-11-01"}'
curl -s -X PUT $M/api/chores/1 -H 'content-type: application/json' -d '{"every":3}'
curl -s -X DELETE $M/api/chores/1
curl -s -X POST $M/api/chores/1/skip -H 'content-type: application/json' -d '{"date":"2026-11-16"}'  # 这天不做，顺延
```

`unit`：`d` 天、`w` 周、`m` 月。到期后排到之后阿姨第一个做饭的日子，每天最多一件。

## 买过的东西和库存

```bash
curl -s -X POST $M/api/purchases -H 'content-type: application/json' \
  -d '{"channel":"manual","items":[{"time":"2026-11-12","name":"土豆","count":3,"ingredient":"土豆","cat":"蔬菜"}]}'
curl -s "$M/api/purchases?days=90&channel=manual"    # DELETE /api/purchases/<id> 删一条
curl -s "$M/api/aliases?unmapped=1"                  # 商品名 → 原料/品类，unmapped=1 只看没对上的；PUT 批量改
curl -s $M/api/purchases/stats                       # 按原料：买过几次、最近哪天、平均几天买一次
curl -s $M/api/stock                                 # 家里有什么（估的）：剩多少、哪天到期、快坏的、原料齐的菜
curl -s -X POST $M/api/stock -H 'content-type: application/json' -d '{"ingredient":"鸡蛋","left":6}'   # 校正，0 = 用完了
curl -s -X POST $M/api/stock/add -H 'content-type: application/json' -d '{"text":"土豆 3个、鸡蛋 10个"}'  # 手记一笔
curl -s "$M/api/stock/dishes?date=2026-11-12"        # 这天阿姨来之前，原料齐或只缺一样的菜
curl -s $M/api/shopping/2026-11-12                   # 这天要买什么：原料汇总（去掉常备调料），have/missing 对着库存
```

**库存是估的**：买一次记成几「份」加一个到期日（叶菜 3 天、根茎 14 天、水产 2 天……），阿姨做饭那天到点后，菜单里每道菜的原料各扣一份。不准就在网页「家里有什么」里改。

## 屏幕和固件

```bash
curl -s $M/api/device                                # 屏幕状态：上次联网、电量/信号格数、估算续航、下次醒来
curl -s "$M/api/logs?kind=device"                    # 屏幕每次请求；kind=edits 是每次保存菜单
curl -s $M/metrics                                   # Prometheus 格式
curl -X PUT $M/api/firmware -H 'X-Fw-Version: 2026.11.12-1' -H 'Authorization: Bearer <MENU_ADMIN_TOKEN>' \
  --data-binary @firmware/.pio/build/epd/firmware.bin    # firmware/publish.sh 做的就是这个
```

屏幕用的（固件调，一般不用管）：`GET /api/frame`（报电量、信号、温湿度，拿 800×480 黑红两层位图，响应头 `X-Sleep` 是睡多久、`X-Fw` 是最新固件版本）、`GET /api/telemetry`、`GET /firmware.bin`。
