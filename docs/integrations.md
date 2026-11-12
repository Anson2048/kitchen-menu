# 通知和外部集成

两个方向：

- **发出去**：服务里发生的事（菜单改了、屏幕没按时醒、该充电了……）发 webhook 给 Home Assistant 等自动化，挑几种推到手机
- **进来**：外部系统调接口加留言、报洗衣机洗完、导入买过的东西

都在网页 **设置 › 通知和集成** 里配置，配好可以点「发一条试试」。

## 事件

| 事件 | 什么时候 | `data` |
| --- | --- | --- |
| `menu_saved` | 网页或接口保存了某天的菜单 | 整天的菜单（`date`、`mode`、`dishes`、`steam`、`tasks`、`note`） |
| `device_seen` | 屏幕醒来联网 | `bat`（mV）、`bat_level`（0～4 格）、`charging`、`rssi`、`wifi_level`、`temp`、`hum`、`fw`、`reason` |
| `device_missed` | 屏幕过了约定时间 30 分钟还没来 | `expected`、`last_seen` |
| `battery_low` | 电量掉到 0 格（没在充电） | `bat` |
| `menu_missing` | 阿姨下次来做饭的前一天，到了提醒时间（默认 20:00）菜单还是空的 | `date` |
| `laundry` | 外部报了洗衣机洗完，问要不要给阿姨加「晒衣服」 | `date`、`text` |

`device_missed`、`menu_missing` 每 5 分钟检查一次，同一件事只发一次。

## Webhook（发出去）

设置里填一个 URL、勾选事件。每个事件 POST 一次 JSON：

```json
{
  "event": "battery_low",
  "time": "2026-11-12T06:00:12",
  "title": "屏幕该充电了",
  "message": "电池 3480 mV，到 0 格了，找时间充一下电",
  "data": {"bat": 3480}
}
```

发送失败不重试，记在 **设置 › 日志** 里。

## 手机通知

| 渠道 | 地址怎么填 | token |
| --- | --- | --- |
| [ntfy](https://ntfy.sh) | `https://ntfy.sh/<你的主题>`，自建的就是 `https://<你的服务器>/<主题>` | 主题设了权限才要填 access token |
| [Bark](https://github.com/Finb/Bark)（iPhone） | `https://api.day.app/<你的 key>`，自建的同理 | 不用 |

默认推 `device_missed`、`battery_low`、`menu_missing`、`laundry`。设置里填了**网页访问地址**的话，点通知会打开网页；ntfy 上「洗衣机洗完」那条还带「加上」按钮，点一下直接给阿姨加留言。

## 接口（进来）

```bash
M=http://<服务器>:8000

# 加一条留言：不给 date 就加到阿姨最近一次来的日子（今天她还没走就是今天）。网页上带「自动」标签
curl -X POST $M/api/hooks/task -H 'content-type: application/json' -d '{"text":"收快递"}'
curl -X POST $M/api/hooks/task -H 'content-type: application/json' -d '{"text":"给猫加粮","date":"2026-11-20"}'

# 洗衣机洗完：算出提醒哪天，发 laundry 事件；没点通知的话网页上那天会出一条建议
curl -X POST $M/api/laundry -H 'content-type: application/json' -d '{}'

# 导入买过的东西（任意来源，channel 自己起名；同一 channel + ref + name 重复导入不会重复记）
curl -X POST $M/api/purchases -H 'content-type: application/json' -d '{
  "channel": "myshop",
  "items": [{"time": "2026-11-12T09:00:00", "ref": "订单号123", "name": "精品五花肉 500g", "count": 1, "price": 25.9}]
}'
```

导入后新商品名还没对上原料，接口会返回 `unmapped`，用 `PUT /api/aliases` 补上，之后库存估算和选菜推荐就会用上。品类从 `GET /api/aliases` 返回的 `cats` 里选（蔬菜、菌菇、猪肉、牛羊肉、禽肉、水产……），日用品、零食只填品类不填原料：

```bash
curl -X PUT $M/api/aliases -H 'content-type: application/json' -d '{"精品五花肉 500g": {"ingredient": "五花肉", "cat": "猪肉"}}'
```

也可以导入时直接在 item 里带上 `ingredient`、`cat`（截图识别、手记时方便）。买菜平台大多没有开放接口，导入脚本要自己写：把订单整理成上面的 `items` 推过来就行。

完整接口见 [api.md](api.md)。

## Home Assistant 示例

### 收服务的事件

HA 里建一个 webhook 触发的自动化，把它的地址填进设置的 webhook URL（`http://<HA>:8123/api/webhook/kitchen_menu`）：

```yaml
automation:
  - alias: 厨房菜单屏该充电了
    trigger:
      - platform: webhook
        webhook_id: kitchen_menu
        allowed_methods: [POST]
        local_only: true
    condition:
      - "{{ trigger.json.event == 'battery_low' }}"
    action:
      - service: notify.mobile_app_你的手机
        data:
          title: "{{ trigger.json.title }}"
          message: "{{ trigger.json.message }}"
```

### 洗衣机洗完提醒晒衣服

洗衣机接入了 HA（智能插座看功率、或者洗衣机自带集成）就行：

```yaml
rest_command:
  kitchen_menu_laundry:
    url: http://<服务器>:8000/api/laundry
    method: POST
    content_type: application/json
    payload: "{}"

automation:
  - alias: 洗衣机洗完告诉菜单
    trigger:
      - platform: state
        entity_id: sensor.washer_state      # 换成你的洗衣机状态
        to: "done"
    action:
      - service: rest_command.kitchen_menu_laundry
```

服务算好该提醒哪天，通过 ntfy 推一条带「加上」按钮的通知；用 HA 自己的通知也可以：把 webhook 事件 `laundry` 转给手机，按钮调 `POST /api/menu/<date>/tasks`（body `{"text":"晒衣服","auto":true}`）。

### 屏幕状态做成传感器

```yaml
rest:
  - resource: http://<服务器>:8000/api/device
    scan_interval: 600
    sensor:
      - name: 厨房菜单屏电量
        value_template: "{{ value_json.bat_level }}"
      - name: 厨房温度
        value_template: "{{ value_json.temp }}"
        unit_of_measurement: "°C"
```

## Prometheus / Grafana

`GET /metrics` 是 Prometheus 文本格式（屏幕上次联网、下次醒来、电压、续航、固件是否过期、下次阿姨来那天录了几道菜……），已经有 Grafana 的话可以在那边做告警，设置里的通知就不用开。
