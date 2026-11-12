// 厨房菜单屏：醒来 → 读电量和温湿度 → 连 WiFi 向服务器要图 → 有变化就刷屏 → 按服务器给的秒数深度睡眠
// 服务器说有新版本就走 WiFi 升级；新固件连续 3 次连不上服务器，自动切回上一版
// WiFi 和服务器地址存在 NVS，没配过就用编译时默认值；都没有，或者按键复位时连不上，就开热点配网
#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <Preferences.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <memory>
#include <esp_ota_ops.h>
#include <esp_task_wdt.h>
#include <GxEPD2_3C.h>
#include <Fonts/FreeSansBold18pt7b.h>
#include <Fonts/FreeSans12pt7b.h>
#include "version.h"
#include "badge.h"

// 编译时默认值，由 scripts/env_defaults.py 从环境变量注入；不用 PlatformIO 编译时就是空的
#ifndef MENU_WIFI_SSID
#define MENU_WIFI_SSID ""
#endif
#ifndef MENU_WIFI_PASS
#define MENU_WIFI_PASS ""
#endif
#ifndef MENU_SERVER
#define MENU_SERVER ""
#endif

#define PIN_BUSY 10
#define PIN_RST 8
#define PIN_DC 2
#define PIN_CS 7
#define PIN_SCK 4
#define PIN_MOSI 6
#define PIN_BAT 0
#define SHT_ADDR 0x44

static const int W = 800, H = 480;
static const uint32_t PORTAL_TIMEOUT_MS = 5 * 60 * 1000;  // 配网页没人操作多久就放弃
static const uint32_t PORTAL_SLEEP_S = 3600;              // 放弃后睡多久

GxEPD2_3C<GxEPD2_750c_Z08, 8> display(GxEPD2_750c_Z08(PIN_CS, PIN_DC, PIN_RST, PIN_BUSY));
Preferences prefs;
RTC_DATA_ATTR char etag[40] = "";
RTC_DATA_ATTR uint8_t failCount = 0;     // 连续几次没联系上服务器
RTC_DATA_ATTR bool offlineShown = false; // 屏幕上是否已经画了「未联网」
RTC_DATA_ATTR bool portalShown = false;  // 屏幕上是否正显示配网提示

// 运行时配置：NVS 里有就用 NVS 的，没有就用编译时默认值
static String cfgSsid, cfgPass, cfgServer;
static bool cfgFromNvs = false;  // 三项是不是都来自 NVS

static String normServer(String s) {
  s.trim();
  while (s.endsWith("/")) s.remove(s.length() - 1);
  String low = s;
  low.toLowerCase();
  if (low.startsWith("http://")) s = "http://" + s.substring(7);
  else if (low.startsWith("https://")) s = "https://" + s.substring(8);
  else if (s.length()) s = "http://" + s;  // 没写协议当 http
  return s;
}

static void loadConfig() {
  bool a = prefs.isKey("ssid"), b = prefs.isKey("pass"), c = prefs.isKey("server");
  cfgSsid = a ? prefs.getString("ssid") : String(MENU_WIFI_SSID);
  cfgPass = b ? prefs.getString("pass") : String(MENU_WIFI_PASS);
  cfgServer = normServer(c ? prefs.getString("server") : String(MENU_SERVER));
  cfgFromNvs = a && b && c;
}

static void saveConfig(const String &ssid, const String &pass, const String &server) {
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.putString("server", server);
}

static bool isHttps(const String &url) { return url.startsWith("https://"); }

// http:// 用普通连接，https:// 用 TLS（不校验证书，见 README）
static std::unique_ptr<WiFiClient> makeClient(const String &url) {
  if (isHttps(url)) {
    WiFiClientSecure *c = new WiFiClientSecure;
    c->setInsecure();
    return std::unique_ptr<WiFiClient>(c);
  }
  return std::unique_ptr<WiFiClient>(new WiFiClient);
}

static uint8_t crc8(const uint8_t *d, int n) {
  uint8_t c = 0xFF;
  for (int i = 0; i < n; i++) {
    c ^= d[i];
    for (int b = 0; b < 8; b++) c = (c & 0x80) ? (c << 1) ^ 0x31 : c << 1;
  }
  return c;
}

// 传感器是 SHT4x（原厂固件反汇编确认：0x44，单字节命令 0xFD，10ms 后读 6 字节）。
// diag 记下每一步：p 只探地址，sn 读序列号，fd 测量，94 软复位；e<n> 是 endTransmission 的错误码，n<n> 是读到的字节数
static String shtDiag;
static int shtCmd(uint8_t cmd, uint8_t *d, int wait) {
  Wire.beginTransmission(SHT_ADDR);
  Wire.write(cmd);
  int e = Wire.endTransmission();
  if (e != 0) return -e;
  if (!d) return 0;
  delay(wait);
  int n = Wire.requestFrom(SHT_ADDR, 6);
  for (int i = 0; i < n && i < 6; i++) d[i] = Wire.read();
  return n;
}
static void shtLog(const char *tag, int r, const uint8_t *d) {
  shtDiag += tag;
  if (r < 0) shtDiag += "e" + String(-r);
  else if (r != 6) shtDiag += "n" + String(r);
  else {
    char hex[16];
    snprintf(hex, sizeof(hex), "%02x%02x%02x%02x%02x%02x", d[0], d[1], d[2], d[3], d[4], d[5]);
    shtDiag += hex;
  }
  shtDiag += ",";
}
static bool readSht(float &t, float &h) {
  uint8_t d[6];
  Wire.beginTransmission(SHT_ADDR);
  shtDiag += "p" + String(Wire.endTransmission()) + ",";
  for (int attempt = 0; attempt < 2; attempt++) {
    if (attempt) {  // 第一次不行：软复位再来
      shtLog("94:", shtCmd(0x94, nullptr, 0), d);
      delay(2);
    }
    int r = shtCmd(0xFD, d, 10);
    shtLog("fd:", r, d);
    if (r != 6) continue;
    if (crc8(d, 2) != d[2] || crc8(d + 3, 2) != d[5]) { shtDiag += "c,"; continue; }
    uint16_t rt = (d[0] << 8) | d[1], rh = (d[3] << 8) | d[4];
    t = -45 + 175.0f * rt / 65535;
    h = constrain(-6 + 125.0f * rh / 65535, 0, 100);
    return true;
  }
  shtLog("sn:", shtCmd(0x89, d, 2), d);
  return false;
}

static void sleepFor(uint32_t sec) {
  sec = constrain(sec, 60, 86400);
  Serial.printf("sleep %u s\n", sec);
  Serial.flush();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  esp_sleep_enable_timer_wakeup((uint64_t)sec * 1000000ULL);
  esp_deep_sleep_start();
}

// 右下角局部刷出红字「未联网」（服务器连不上时只能自己画）
static void drawOffline() {
  static uint8_t black[BADGE_W / 8 * BADGE_H];
  static uint8_t red[BADGE_W / 8 * BADGE_H];
  memset(black, 0xFF, sizeof(black));  // 黑层全白，盖掉原来的「更新于」
  memcpy_P(red, badgeRed, sizeof(red));
  SPI.begin(PIN_SCK, -1, PIN_MOSI, PIN_CS);
  display.init(0, false, 2, false);
  display.epd2.writeImage(black, red, BADGE_X, BADGE_Y, BADGE_W, BADGE_H);
  display.epd2.refresh(BADGE_X, BADGE_Y, BADGE_W, BADGE_H);
  display.epd2.hibernate();
  SPI.end();
  Serial.println("offline badge drawn");
}

// 没联系上服务器：连续两次就在屏幕上标出来，然后过一阵再试
static void failAndSleep(const char *why) {
  Serial.println(why);
  if (failCount < 255) failCount++;
  if (failCount >= 2 && !offlineShown) {
    drawOffline();
    offlineShown = true;
  }
  sleepFor(failCount < 4 ? 1800 : 3600);
}

// 升级后的新固件还没被确认能用：每次启动记一次，连续 3 次没联系上服务器就切回旧分区
static void checkRollback() {
  if (!prefs.getBool("pending", false)) return;
  int tries = prefs.getInt("tries", 0) + 1;
  prefs.putInt("tries", tries);
  Serial.printf("new firmware unverified, try %d\n", tries);
  if (tries > 3) {
    const esp_partition_t *old = esp_ota_get_next_update_partition(NULL);
    prefs.putString("badfw", FW_VERSION);
    prefs.putBool("pending", false);
    if (old && esp_ota_set_boot_partition(old) == ESP_OK) {
      Serial.println("rollback");
      ESP.restart();
    }
  }
}

// ---------------- 配网模式 ----------------
// 开热点 Menu-Setup-XXXX（XXXX 是 MAC 后 4 位）+ captive portal，网页上填 WiFi 和服务器地址，存进 NVS 后重启。
// 没人操作超过 PORTAL_TIMEOUT_MS 就深度睡眠 PORTAL_SLEEP_S，免得耗光电池

static WebServer web(80);
static DNSServer dns;
static String apName, scanOptions;
static uint32_t portalLastActive;
static bool portalSaved = false;

static String htmlEscape(const String &s) {
  String o;
  o.reserve(s.length() + 8);
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '&') o += "&amp;";
    else if (c == '<') o += "&lt;";
    else if (c == '>') o += "&gt;";
    else if (c == '"') o += "&quot;";
    else if (c == '\'') o += "&#39;";
    else o += c;
  }
  return o;
}

// 屏幕上画配网提示（GFX 自带字体只有 ASCII，用英文 + 拼音）
static void drawPortalScreen(const char *reason) {
  SPI.begin(PIN_SCK, -1, PIN_MOSI, PIN_CS);
  display.init(0, true, 2, false);
  display.setRotation(0);
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setFont(&FreeSansBold18pt7b);
    display.setCursor(40, 70);
    display.print("WiFi setup  (pei wang)");
    display.setFont(&FreeSans12pt7b);
    display.setCursor(40, 140);
    display.print("1. Connect your phone to WiFi:");
    display.setFont(&FreeSansBold18pt7b);
    display.setTextColor(GxEPD_RED);
    display.setCursor(80, 190);
    display.print(apName);
    display.setFont(&FreeSans12pt7b);
    display.setTextColor(GxEPD_BLACK);
    display.setCursor(40, 250);
    display.print("2. The setup page should pop up. If not, open:");
    display.setFont(&FreeSansBold18pt7b);
    display.setCursor(80, 300);
    display.print("http://192.168.4.1");
    display.setFont(&FreeSans12pt7b);
    display.setCursor(40, 360);
    display.print("3. Fill in WiFi name, password and server address.");
    display.setCursor(40, 420);
    display.printf("Reason: %s.  Gives up after %u min, press the front", reason,
                   (unsigned)(PORTAL_TIMEOUT_MS / 60000));
    display.setCursor(40, 450);
    display.print("button to try again.   fw " FW_VERSION);
  } while (display.nextPage());
  display.hibernate();
  SPI.end();
}

static void portalPage() {
  portalLastActive = millis();
  String h;
  h.reserve(3000);
  h += F("<!doctype html><html><head><meta charset=utf-8>"
         "<meta name=viewport content='width=device-width,initial-scale=1'><title>菜单屏配网</title>"
         "<style>body{font-family:sans-serif;max-width:420px;margin:20px auto;padding:0 12px}"
         "label{display:block;margin-top:14px}input{width:100%;box-sizing:border-box;padding:8px;font-size:16px}"
         "button{margin-top:20px;width:100%;padding:12px;font-size:17px}small{color:#666}</style></head><body>"
         "<h2>菜单屏配网</h2><form method=post action=/save>"
         "<label>WiFi 名称<input name=ssid list=nets required value=\"");
  h += htmlEscape(cfgSsid);
  h += F("\"></label><datalist id=nets>");
  h += scanOptions;
  h += F("</datalist><label>WiFi 密码<input name=pass type=password placeholder='");
  h += cfgPass.length() ? F("留空表示不改") : F("没有密码就留空");
  h += F("'></label><label>服务器地址<input name=server required placeholder='http://192.168.1.10:8000' value=\"");
  h += htmlEscape(cfgServer);
  h += F("\"></label><small>http:// 或 https://，不要带路径</small>"
         "<button type=submit>保存并重启</button></form><p><small>");
  h += htmlEscape(apName) + " · fw " FW_VERSION " · MAC " + WiFi.macAddress();
  h += F("</small></p></body></html>");
  web.send(200, "text/html; charset=utf-8", h);
}

static void portalSave() {
  portalLastActive = millis();
  String ssid = web.arg("ssid"), pass = web.arg("pass"), server = normServer(web.arg("server"));
  if (!ssid.length() || ssid.length() > 32 || pass.length() > 64 || !server.length()) {
    web.send(400, "text/html; charset=utf-8",
             "<meta charset=utf-8><p>WiFi 名称和服务器地址必填（名称最长 32 字节，密码最长 64）。<a href=/>返回</a>");
    return;
  }
  if (!pass.length() && ssid == cfgSsid) pass = cfgPass;  // 同一个 WiFi、密码留空：沿用原来的
  saveConfig(ssid, pass, server);
  web.send(200, "text/html; charset=utf-8",
           "<meta charset=utf-8><meta name=viewport content='width=device-width'>"
           "<h3>已保存，屏幕正在重启</h3><p>大约一分钟后屏幕会显示菜单。没出来就按一下屏幕正面的键再配一次。");
  portalSaved = true;
}

// 系统的联网检测（/generate_204、/hotspot-detect.html 等）一律跳到配网页，手机就会弹出来
static void portalRedirect() {
  portalLastActive = millis();
  web.sendHeader("Location", "http://192.168.4.1/", true);
  web.send(302, "text/plain", "");
}

[[noreturn]] static void runPortal(const char *reason) {
  Serial.printf("portal: %s\n", reason);
  esp_task_wdt_reset();
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char name[32];
  snprintf(name, sizeof(name), "Menu-Setup-%02X%02X", mac[4], mac[5]);
  apName = name;

  // 屏幕已经是配网提示（比如没配过、定时醒来又进来）就不重画，省电
  if (!portalShown) {
    drawPortalScreen(reason);
    portalShown = true;
    offlineShown = false;
    etag[0] = 0;  // 配好后第一次联网必须整屏重画盖掉提示
  }
  esp_task_wdt_reset();

  // 先扫一遍附近的 WiFi 给网页做候选
  WiFi.disconnect(true);
  WiFi.mode(WIFI_AP_STA);
  int n = WiFi.scanNetworks();
  scanOptions = "";
  for (int i = 0; i < n && i < 20; i++) {
    String s = WiFi.SSID(i);
    if (s.length() && scanOptions.indexOf("\"" + htmlEscape(s) + "\"") < 0)
      scanOptions += "<option value=\"" + htmlEscape(s) + "\">";
  }
  WiFi.scanDelete();
  esp_task_wdt_reset();

  WiFi.softAPConfig(IPAddress(192, 168, 4, 1), IPAddress(192, 168, 4, 1), IPAddress(255, 255, 255, 0));
  WiFi.softAP(apName.c_str());  // 开放热点，只在配网的几分钟里开着
  dns.setErrorReplyCode(DNSReplyCode::NoError);
  dns.start(53, "*", IPAddress(192, 168, 4, 1));
  web.on("/", HTTP_GET, portalPage);
  web.on("/save", HTTP_POST, portalSave);
  web.onNotFound(portalRedirect);
  web.begin();
  Serial.printf("AP %s up\n", apName.c_str());

  portalLastActive = millis();
  while (true) {
    esp_task_wdt_reset();  // 配网可能要好几分钟，一直喂狗
    dns.processNextRequest();
    web.handleClient();
    if (portalSaved) {
      delay(1500);  // 让「已保存」页面发出去
      web.stop();
      dns.stop();
      ESP.restart();
    }
    // 只有网页请求才算有人在操作；手机光连着热点不算（有的手机会一直挂着开放热点）
    if (millis() - portalLastActive > PORTAL_TIMEOUT_MS) {
      Serial.println("portal timeout");
      web.stop();
      dns.stop();
      WiFi.softAPdisconnect(true);
      sleepFor(PORTAL_SLEEP_S);
    }
    delay(2);
  }
}

static bool readExact(WiFiClient *s, uint8_t *buf, size_t n) {
  size_t got = 0;
  uint32_t t0 = millis();
  while (got < n && millis() - t0 < 20000) {
    int r = s->read(buf + got, n - got);
    if (r > 0) { got += r; t0 = millis(); } else delay(5);
  }
  return got == n;
}

static uint8_t bandBlack[W / 8 * 40], bandRed[W / 8 * 40];

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);
  esp_task_wdt_init(150, true);  // 整个周期正常一分钟内完成，卡死就重启（会计入回滚次数）
  esp_task_wdt_add(NULL);
  prefs.begin("menu", false);
  checkRollback();

  bool timerWake = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER;
  if (!timerWake) etag[0] = 0;  // 按键复位或上电：强制重刷一次
  if (offlineShown) etag[0] = 0;  // 之前画了「未联网」，这次连上要整屏重画盖掉
  loadConfig();
  Serial.printf("fw %s, wake %s, ssid %s, server %s (%s)\n", FW_VERSION, timerWake ? "timer" : "reset",
                cfgSsid.length() ? "set" : "-", cfgServer.c_str(), cfgFromNvs ? "nvs" : "built-in");
  // 没配过 WiFi 或服务器：不管怎么醒的都只能配网
  if (!cfgSsid.length() || !cfgServer.length()) runPortal("not configured");

  int bat = analogReadMilliVolts(PIN_BAT) * 2;
  float t = -1000, h = -1;
  Wire.begin(PIN_MOSI, PIN_SCK, 100000);  // 和屏幕 SPI 共用这两根线，先读完再释放
  delay(5);
  readSht(t, h);
  Wire.end();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname("menu-epd");
  // 家里多台 AP 同名：扫全部信道，连信号最强的那台（默认是扫到第一台就连）
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
  WiFi.begin(cfgSsid.c_str(), cfgPass.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(100);
  if (WiFi.status() != WL_CONNECTED) {
    // 有人按了键（或刚上电）就开热点让人重新配；定时醒来还是照旧过一阵再试
    if (!timerWake) runPortal("WiFi connect failed");
    failAndSleep("wifi failed");
  }

  std::unique_ptr<WiFiClient> client = makeClient(cfgServer);
  HTTPClient http;
  char url[400];
  snprintf(url, sizeof(url), "%s/api/frame?bat=%d&rssi=%d&t=%.1f&h=%.1f&fw=%s&reason=%s&diag=%s", cfgServer.c_str(),
           bat, WiFi.RSSI(), t, h, FW_VERSION, timerWake ? "timer" : "reset", shtDiag.c_str());
  http.begin(*client, url);
  http.setTimeout(20000);
  if (etag[0]) http.addHeader("If-None-Match", etag);
  const char *keys[] = {"ETag", "X-Sleep", "X-Fw", "X-Band"};
  http.collectHeaders(keys, 4);
  int code = http.GET();
  Serial.printf("GET %d\n", code);
  if (code != 200 && code != 304) {
    http.end();
    // 按键醒来时连不上服务器或 404，多半是地址填错了，给机会重新配；服务器报 5xx 之类就不打扰
    if (!timerWake && (code < 0 || code == 404)) runPortal("server unreachable");
    failAndSleep("server failed");
  }

  failCount = 0;
  // 联系上服务器了，说明这个固件能用
  if (prefs.getBool("pending", false)) prefs.putBool("pending", false);
  // 编译时默认值能用就存进 NVS，以后 OTA 的固件不带 WiFi 密码也能连
  if (!cfgFromNvs) saveConfig(cfgSsid, cfgPass, cfgServer);
  esp_task_wdt_reset();

  uint32_t sleep = http.header("X-Sleep").toInt();
  String fw = http.header("X-Fw");
  int band = http.header("X-Band").toInt();
  String newTag = http.header("ETag");

  if (code == 200 && band == 40) {
    WiFiClient *s = http.getStreamPtr();
    SPI.begin(PIN_SCK, -1, PIN_MOSI, PIN_CS);
    display.init(0, true, 2, false);
    bool ok = true;
    for (int y = 0; y < H && ok; y += band) {
      ok = readExact(s, bandBlack, sizeof(bandBlack)) && readExact(s, bandRed, sizeof(bandRed));
      if (ok) display.epd2.writeImage(bandBlack, bandRed, 0, y, W, band);
    }
    http.end();
    if (ok) {
      display.epd2.refresh(false);
      strlcpy(etag, newTag.c_str(), sizeof(etag));
      offlineShown = false;
      portalShown = false;
    }
    display.epd2.hibernate();
    SPI.end();
    Serial.printf("drawn ok=%d\n", ok);
  } else {
    http.end();
  }
  esp_task_wdt_reset();

  // 开机时没读到，刷完屏再试一次；成败都把诊断报上去
  if (t < -100) {
    for (int pass = 0; pass < 2 && t < -100; pass++) {  // 先原速，再降到 20kHz（飞线长、和屏幕共线，看是不是信号问题）
      Wire.begin(PIN_MOSI, PIN_SCK, pass ? 20000 : 100000);
      delay(10);
      shtDiag += pass ? "|slow:" : "|scan:";
      if (!pass)
        for (uint8_t a = 8; a < 120; a++) {
          Wire.beginTransmission(a);
          if (Wire.endTransmission() == 0) shtDiag += String(a, HEX) + ",";
        }
      shtDiag += "|";
      readSht(t, h);
      Wire.end();
    }
    char tu[400];
    snprintf(tu, sizeof(tu), "%s/api/telemetry?t=%.1f&h=%.1f&diag=%s", cfgServer.c_str(), t, h, shtDiag.c_str());
    HTTPClient th;
    std::unique_ptr<WiFiClient> tc = makeClient(cfgServer);
    th.begin(*tc, tu);
    th.GET();
    th.end();
  }

  if (fw.length() && fw != FW_VERSION && fw != prefs.getString("badfw", "")) {
    Serial.printf("update %s -> %s\n", FW_VERSION, fw.c_str());
    httpUpdate.rebootOnUpdate(false);
    String fwUrl = cfgServer + "/firmware.bin";
    std::unique_ptr<WiFiClient> uc = makeClient(fwUrl);
    t_httpUpdate_return r = httpUpdate.update(*uc, fwUrl);
    if (r == HTTP_UPDATE_OK) {
      prefs.putBool("pending", true);
      prefs.putInt("tries", 0);
      ESP.restart();
    }
    Serial.printf("update failed: %s\n", httpUpdate.getLastErrorString().c_str());
  }

  sleepFor(sleep ? sleep : 3600);
}

void loop() {}
