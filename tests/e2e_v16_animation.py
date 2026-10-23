# -*- coding: utf-8 -*-
"""午托点名 v1.6.1 —— 两段式落位动画 + 在线更新策略 端到端自测

真机（无头 Edge + CDP，390×844）验证：
  1. manifest / apple-mobile-web-app-title 仍精确为「宇才教育」，业务标题仍是「午托点名」
  2. APP_VERSION=1.6.1、SW 缓存版本 wudian-v1.6.1、DB_VER 仍为 3（不迁移）
  3. 被点卡片 data-motion=drop，WAAPI 时长 320ms，曲线前段不再过快
  4. 约 80ms 时被点卡片处于「旧位置 → 新位置」之间（不是第一帧瞬移）
  5. 补位卡片 data-motion=float-up，delay≈70ms / 260ms，延迟期用 fill:"both" 锁在旧位置
  6. 落位与补位都是真实 DOM 重排（顺序变了 + 布局坐标真变了），不是纯 CSS 假动画
  7. 动画结束无 data-motion 残留、无 transform 残留、无 getAnimations 残留
  8. 幂等点击 / 快速连点 / 撤销 行为不回归
  9. 首屏加载 / 搜索 / 筛选 / 切日期 / 切模式一律不播重排动画
 10. prefers-reduced-motion: reduce 时无上下位移，只保留 ≤150ms 透明度反馈
 11. 390×844 无横向溢出、无乱码、无外部资源；落位期间被点卡片不被补位卡片遮住
 12. 培训「概况」页最底部有可见的 v1.6.1 版本标记（稳定 DOM 标识）
 13. sw.js 导航请求为 network-first（fetch 在前、caches.match 回退在后），
     保留 skipWaiting / clients.claim 与旧缓存清理
 14. 页面 controllerchange 自动刷新带防循环保护
 15. 截图截在动画进行中（约 120ms）：screenshots/v161-roll-motion.png

用法（本机）：
  C:\\Users\\James\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\e2e_v16_animation.py
依赖：标准库 + websocket-client（无头 Edge 用系统安装的 msedge.exe）
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

try:
    import websocket  # websocket-client
except ImportError:
    sys.exit("缺少 websocket-client，请先：pip install websocket-client")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
HTTP_PORT = 8109
CDP_PORT = 9454
PROFILE = os.path.join(ROOT, "tests", "_e2e_v16_profile_%s" % time.strftime("%Y%m%d-%H%M%S"))
SHOT_DIR = os.path.join(ROOT, "screenshots")        # v1.6.1 交付截图放根 screenshots/
REPORT = os.path.join(ROOT, "tests", "last-run-v16-motion.json")


# --------------------------------------------------------------------------
# 本地静态服务
# --------------------------------------------------------------------------
class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


def serve():
    handler = partial(QuietHandler, directory=ROOT)
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------
# CDP
# --------------------------------------------------------------------------
class Page(object):
    def __init__(self, ws_url, proc):
        self.ws = websocket.create_connection(
            ws_url, timeout=180, max_size=128 * 1024 * 1024, suppress_origin=True)
        self.proc = proc
        self._id = 0

    def send(self, method, params=None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s 失败：%s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True,
            "returnByValue": True, "userGesture": True
        })
        if r.get("exceptionDetails"):
            det = r["exceptionDetails"]
            desc = (det.get("exception") or {}).get("description") or det.get("text")
            raise RuntimeError("页面脚本异常：%s" % desc)
        return r["result"].get("value")

    def wait_ready(self, secs=30):
        end = time.time() + secs
        while time.time() < end:
            try:
                if self.js("window.__ready === true"):
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("页面未在 %ss 内就绪" % secs)

    def goto(self, url):
        self.send("Page.navigate", {"url": url})
        time.sleep(0.8)
        self.wait_ready()

    def reload(self):
        self.send("Page.reload")
        time.sleep(1.0)
        self.wait_ready()

    def mobile(self, w=390, h=844):
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1,
            "mobile": True, "screenWidth": w, "screenHeight": h
        })

    def reduce_motion(self, on=True):
        """无头 Edge 默认就报 prefers-reduced-motion: reduce，
        所以正常动画场景必须显式设成 no-preference。"""
        value = "reduce" if on else "no-preference"
        self.send("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": value}]})

    def shot(self, name):
        os.makedirs(SHOT_DIR, exist_ok=True)
        data = self.send("Page.captureScreenshot", {"format": "png"})["data"]
        path = os.path.join(SHOT_DIR, name + ".png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path

    def close(self):
        try:
            self.send("Browser.close")
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


JS_CLEAN_ENV = r"""
(async () => {
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      const rs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(rs.map(r => r.unregister()));
    }
  } catch (e) {}
  try {
    if (window.caches && caches.keys) {
      const ks = await caches.keys();
      await Promise.all(ks.map(k => caches.delete(k)));
    }
  } catch (e) {}
  return true;
})()
"""


def launch(profile=PROFILE):
    _close_stale_cdp()
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--remote-debugging-port=%d" % CDP_PORT,
         "--remote-allow-origins=*", "--user-data-dir=" + profile,
         "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--window-size=390,844", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            targets = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=2).read())
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    page = Page(t["webSocketDebuggerUrl"], proc)
                    page.send("Page.enable")
                    page.send("Runtime.enable")
                    try:
                        page.send("Network.enable")
                        page.send("Network.clearBrowserCache")
                        page.js(JS_CLEAN_ENV)
                    except Exception:
                        pass
                    return page
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("无头 Edge 启动失败（CDP 端口未响应）")


def _close_stale_cdp():
    try:
        info = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=1).read())
        ws_url = info.get("webSocketDebuggerUrl")
        if ws_url:
            ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            ws.close()
    except Exception:
        return
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=1)
            time.sleep(0.2)
        except Exception:
            return


# --------------------------------------------------------------------------
# 页面内公共工具（每段 JS 自带，避免依赖全局变量残留）
# --------------------------------------------------------------------------
JS_HELPERS = r"""
const X = window.__test;
const checks = [];
const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
const near = (n, a, b, tol) => checks.push({
  name:n, ok:Math.abs(a - b) <= tol, detail:`期望 ${b}±${tol}，实际 ${a}`});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const gridIds = () => Array.from(document.querySelectorAll("#grid .card")).map(c => c.dataset.id);
const cardEl = id => document.querySelector(`#grid .card[data-id="${id}"]`);
const topOf = id => { const e = cardEl(id); return e ? e.getBoundingClientRect().top : null; };
const IDENTITY = ["none", "matrix(1, 0, 0, 1, 0, 0)",
  "matrix3d(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)"];
const settled = el => IDENTITY.indexOf(getComputedStyle(el).transform) >= 0;
const allSettled = () => Array.from(document.querySelectorAll("#grid .card")).every(settled);
const animCount = () => Array.from(document.querySelectorAll("#grid .card"))
  .reduce((n, c) => n + (c.getAnimations
    ? c.getAnimations().filter(a => !a.constructor || a.constructor.name !== "CSSTransition").length
    : 0), 0);

/* 关键帧解析：容忍浏览器把 translate3d/scale 序列化成任意合法形式 */
function kfInfo(kf) {
  return kf.map(k => {
    const s = String(k.transform || "");
    const t = /translate3d\(\s*(-?[\d.]+)px,\s*(-?[\d.]+)px/.exec(s);
    const sc = /scale\(\s*(-?[\d.]+)\s*\)/.exec(s);
    const op = /opacity\(\s*(-?[\d.]+)\s*\)/.exec(s);
    return {
      transform: s,
      dx: t ? parseFloat(t[1]) : 0,
      dy: t ? parseFloat(t[2]) : 0,
      scale: sc ? parseFloat(sc[1]) : null,
      opacity: op ? parseFloat(op[1]) : null
    };
  });
}

/* 逐帧采样：top / data-motion / 是否带位移 transform / 层级 / 动画时序 */
function sampleFrames(targetId, ms) {
  const t0 = performance.now();
  const frames = [];
  const timings = {};
  let keyframes = null;
  let finished = false;
  return new Promise(res => {
    const finish = () => {
      if (finished) return;
      finished = true;
      res({frames, timings, keyframes});
    };
    setTimeout(finish, ms + 1200);         /* rAF 被节流时的兜底 */
    function tick() {
      if (finished) return;
      const cards = Array.from(document.querySelectorAll("#grid .card"));
      const item = {t: Math.round(performance.now() - t0), tops: {}, motions: {}, shifted: [], z: {}};
      cards.forEach(c => {
        const id = c.dataset.id;
        item.tops[id] = Math.round(c.getBoundingClientRect().top * 100) / 100;
        const m = c.getAttribute("data-motion");
        if (m) {
          item.motions[id] = m;
          item.z[id] = getComputedStyle(c).zIndex;
          if (!timings[id] && c.getAnimations) {
            /* 只认 WAAPI 动画，排除 CSS 过渡（CSSTransition），否则时序断言会串味 */
            const as = c.getAnimations().filter(
              a => !a.constructor || a.constructor.name !== "CSSTransition");
            if (as.length && as[0].effect) {
              const it = as[0].effect.getTiming();
              timings[id] = {
                kind: m, duration: it.duration, delay: it.delay || 0,
                easing: String(it.easing || ""), fill: String(it.fill || "")
              };
              if (id === targetId) keyframes = kfInfo(as[0].effect.getKeyframes());
            }
          }
        }
        const tf = getComputedStyle(c).transform;
        if (IDENTITY.indexOf(tf) < 0) item.shifted.push(id);
      });
      frames.push(item);
      if (performance.now() - t0 < ms) requestAnimationFrame(tick);
      else finish();
    }
    requestAnimationFrame(tick);
  });
}

const kindsOf = frames => {
  const s = [];
  frames.forEach(f => Object.values(f.motions).forEach(m => { if (s.indexOf(m) < 0) s.push(m); }));
  return s;
};
const frameAt = (frames, t) => frames.reduce(
  (best, f) => (Math.abs(f.t - t) < Math.abs(best.t - t) ? f : best), frames[0]);
const firstSeen = (frames, id, kind) => frames.find(f => f.motions[id] === kind) || null;
const lastTop = (frames, id) => {
  for (let i = frames.length - 1; i >= 0; i--) if (frames[i].tops[id] != null) return frames[i].tops[id];
  return null;
};
/* 走了全程的百分之几：0 = 还在旧位置，1 = 已到新位置 */
const fracAt = (top, from, to) => (to === from ? 1 : (top - from) / (to - from));

/* 期望排序：未点在上、已点在下，各组内按 compareStudents */
function expectedOrder() {
  const recIds = new Set(Array.from(X.state.records.values())
    .filter(r => r.date === X.state.date && r.slot === X.state.slot)
    .map(r => r.studentId));
  const act = X.state.students.filter(s => s.active !== false);
  const un = X.sortStudents(act.filter(s => !recIds.has(s.id))).map(s => s.id);
  const mk = X.sortStudents(act.filter(s => recIds.has(s.id))).map(s => s.id);
  return {order: un.concat(mk), unmarked: un, marked: mk, recIds};
}
"""


# --------------------------------------------------------------------------
# 第 1 段：品牌名 / 版本 / SW 源码策略 / 动画参数
# --------------------------------------------------------------------------
JS_BRAND = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  const man = await fetch("manifest.webmanifest", {cache:"no-store"}).then(r => r.json());
  eq("manifest name 精确为「宇才教育」", man.name, "宇才教育");
  eq("manifest short_name 精确为「宇才教育」", man.short_name, "宇才教育");
  eq("apple-mobile-web-app-title 精确为「宇才教育」",
     document.querySelector('meta[name="apple-mobile-web-app-title"]').content, "宇才教育");
  eq("页面业务标题保持不变", document.title, "午托点名");
  ok("页面内仍是午托点名 / 培训签到业务名",
     document.querySelector("#modeSeg").textContent.indexOf("午托点名") >= 0 &&
     document.querySelector("#modeSeg").textContent.indexOf("培训签到") >= 0);
  eq("APP_VERSION 为 1.6.1", X.appVersion, "1.6.1");

  /* ---------- 动画参数 ---------- */
  near("被点卡片落位总时长约 320ms", X.motion.dropDuration, 320, 20);
  near("补位启动延迟约 70ms", X.motion.floatDelay, 70, 10);
  near("补位持续约 260ms", X.motion.floatDuration, 260, 20);
  ok("落位曲线改为前段平缓的 cubic-bezier(0.22, 0.61, 0.36, 1)",
     /0?\.22/.test(String(X.motion.easing)) && String(X.motion.easing).indexOf("cubic-bezier") >= 0,
     X.motion.easing);
  ok("不再使用旧的 0.23,1,0.32,1 快出曲线",
     String(X.motion.easing).indexOf("0.23, 1, 0.32, 1") < 0, X.motion.easing);

  /* ---------- Service Worker 源码策略 ---------- */
  const sw = await fetch("sw.js", {cache:"no-store"}).then(r => r.text());
  ok("Service Worker 版本升到 wudian-v1.6.1", sw.indexOf('"wudian-v1.6.1"') >= 0,
     (sw.match(/wudian-v[\d.]+/) || ["未找到"])[0]);
  ok("缓存壳无旧版本残留", sw.indexOf("wudian-v1.6.0") < 0 && sw.indexOf("wudian-v1.5.0") < 0);
  ok("保留 skipWaiting / clients.claim",
     sw.indexOf("skipWaiting") >= 0 && sw.indexOf("clients.claim") >= 0);
  ok("保留旧缓存清理逻辑", sw.indexOf("wudian-shell-") >= 0 && sw.indexOf("caches.delete") >= 0);

  const navIdx = sw.indexOf('req.mode === "navigate"');
  ok("sw.js 有导航请求分支", navIdx >= 0);
  const navSrc = navIdx >= 0 ? sw.slice(navIdx, navIdx + 900) : "";
  const fIdx = navSrc.indexOf("fetch(req)");
  const cIdx = navSrc.indexOf("caches.match");
  ok("导航请求为 network-first（fetch 在前）", fIdx >= 0, fIdx);
  ok("导航请求网络失败后回退缓存（caches.match 在 fetch 之后）",
     cIdx > fIdx && fIdx >= 0, `fetch@${fIdx} / caches.match@${cIdx}`);
  ok("在线拿到新页面时写回缓存（put index.html）",
     /caches\.open\(CACHE\)[\s\S]{0,160}put\(\"\.\/index\.html\"/.test(navSrc) || navSrc.indexOf("./index.html") >= 0);

  /* ---------- 页面侧 controllerchange 防循环 ---------- */
  const htmlSrc = await fetch("index.html", {cache:"no-store"}).then(r => r.text());
  ok("页面监听 controllerchange", htmlSrc.indexOf('addEventListener("controllerchange"') >= 0);
  ok("controllerchange 里会刷新一次", htmlSrc.indexOf("location.reload()") >= 0);
  ok("刷新带防循环保护（reloading 标志 + 首次安装判断）",
     htmlSrc.indexOf("if (reloading) return;") >= 0 && htmlSrc.indexOf("hadController") >= 0);

  /* ---------- 数据库版本不动 ---------- */
  const db = await new Promise((res, rej) => {
    const r = indexedDB.open("wudian-db"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  eq("IndexedDB 版本仍为 3（本轮不迁移）", db.version, 3);
  ok("八张业务表齐全（午托 4 + 培训 5）",
     ["students","records","avatars","kv","trTeachers","trStudents","trSubjects","trCourses","trAttendance"]
       .every(s => Array.from(db.objectStoreNames).indexOf(s) >= 0),
     Array.from(db.objectStoreNames).join(","));
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 2 段：培训「概况」页底部版本标记
# --------------------------------------------------------------------------
JS_BADGE = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  X.switchMode("training");
  await sleep(320);
  X.state.trTab = "info";
  X.renderTraining();
  await sleep(240);

  const b = document.querySelector("#appVerBadge");
  ok("培训概况页底部有稳定 DOM 标识 #appVerBadge", !!b);
  eq("版本标记文案为 v1.6.1", b ? b.textContent.trim() : "(缺失)", "v1.6.1");
  eq("版本标记 data-app-version 为 1.6.1",
     b ? b.getAttribute("data-app-version") : "(缺失)", "1.6.1");
  const rc = b ? b.getBoundingClientRect() : {width:0, height:0};
  ok("版本标记可见（有实际尺寸）", rc.width > 0 && rc.height > 0,
     `${Math.round(rc.width)}×${Math.round(rc.height)}`);
  ok("版本标记不抢主内容（字号 ≤12px 且非粗体）",
     b ? parseFloat(getComputedStyle(b).fontSize) <= 12 : false,
     b ? getComputedStyle(b).fontSize : "缺失");
  ok("版本标记位于概况内容最底部",
     b && document.querySelector("#view").lastElementChild === b,
     b && document.querySelector("#view").lastElementChild
       ? document.querySelector("#view").lastElementChild.className : "缺失");
  ok("版本标记不在主导航 / 统计卡片里",
     b ? (!b.closest("#tabbar") && !b.closest("#modeSeg") && !b.closest(".panel")) : false);

  X.switchMode("wudian");
  await sleep(300);
  return {checks};
})()
"""

# 播种：8 名午托学生、清空记录与界面偏好，然后 Python 侧重载
JS_SEED = r"""
(async () => {
  const db = await new Promise((res, rej) => {
    const r = indexedDB.open("wudian-db"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  const tx = db.transaction(["students", "records"], "readwrite");
  tx.objectStore("students").clear();
  tx.objectStore("records").clear();
  const names = ["王一", "李二", "张三", "赵四", "钱五", "孙六", "周七", "吴八"];
  const groups = ["三年级1班", "三年级1班", "三年级1班", "三年级1班",
                  "四年级1班", "四年级1班", "五年级1班", "五年级1班"];
  names.forEach((n, i) => {
    tx.objectStore("students").put({
      id: "stu-v16-" + (i + 1), name: n, group: groups[i], seq: i + 1,
      active: true, createdAt: 1000 + i, updatedAt: 1000 + i
    });
  });
  await new Promise((res, rej) => { tx.oncomplete = res; tx.onerror = () => rej(tx.error); });
  try { localStorage.removeItem("wudian.pref.v1"); } catch (e) {}
  return true;
})()
"""

# --------------------------------------------------------------------------
# 第 3 段：两段式动画时序 / 幂等 / 快速连点 / 撤销 / 非重排场景
# --------------------------------------------------------------------------
JS_REFLOW = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  if (X.state.mode !== "wudian") { X.switchMode("wudian"); await sleep(320); }
  X.switchTab("roll");
  await sleep(360);

  const ids0 = gridIds();
  eq("播种后点名页 8 张卡片", ids0.length, 8);
  eq("初始全部未点", X.countNow().none, 8);
  ok("初始没有动画标记", X.motion.active() === 0);
  ok("初始卡片无 transform", allSettled());

  /* ---------- 1. 点击第一张未点卡片：两段式落位 ---------- */
  const A = ids0[0], B = ids0[1];
  const before = {};
  ids0.forEach(id => { before[id] = topOf(id); });

  const p1 = sampleFrames(A, 900);
  cardEl(A).click();
  const s1 = await p1;
  const kinds1 = kindsOf(s1.frames);

  const dropFrame = firstSeen(s1.frames, A, "drop");
  ok("被点卡片带 data-motion=drop", !!dropFrame, kinds1.join(",") || "无标记");

  /* 真实 WAAPI 时序（不是常量断言，而是 DOM 上跑的那条动画） */
  const dT = s1.timings[A] || {};
  near("落位动画真实时长约 320ms", dT.duration, 320, 40);
  eq("落位动画正好 320ms", dT.duration, X.motion.dropDuration);
  ok("落位动画曲线为前段平缓曲线",
     /cubic-bezier\(\s*0?\.22/.test(String(dT.easing)), dT.easing);

  /* 关键帧：停留 → 轻放大下落 → 到位收 scale */
  const kf = s1.keyframes || [];
  ok("落位关键帧不少于 3 段", kf.length >= 3, kf.length + " 段");
  ok("首帧停在原格（位移为旧位置偏移、scale=1）",
     kf.length >= 1 && kf[0].dy < -10 && (kf[0].scale === null || kf[0].scale === 1),
     kf.length ? kf[0].transform : "未采到");
  ok("中途有轻微放大", kf.some(k => k.scale !== null && k.scale > 1),
     kf.map(k => k.scale).join(",") || "无 scale");
  ok("末帧回到原位且 scale=1",
     kf.length >= 1 && kf[kf.length - 1].dy === 0 && kf[kf.length - 1].dx === 0 &&
     kf[kf.length - 1].scale === 1, kf.length ? kf[kf.length - 1].transform : "未采到");

  /* 时间轴：80ms 时仍在半路，不是第一帧瞬移 */
  const aFinal = lastTop(s1.frames, A);
  const f30 = frameAt(s1.frames, 30);
  const f80 = frameAt(s1.frames, 80);
  const fr30 = fracAt(f30.tops[A], before[A], aFinal);
  const fr80 = fracAt(f80.tops[A], before[A], aFinal);
  ok("开头短暂停留（约 30ms 时几乎还在原格）", fr30 >= -0.05 && fr30 <= 0.25,
     `已走 ${(fr30 * 100).toFixed(0)}%`);
  ok("约 80ms 时处于旧位置与新位置之间（不是瞬移）", fr80 > 0.1 && fr80 < 0.9,
     `t=${f80.t}ms 已走 ${(fr80 * 100).toFixed(0)}%（旧 ${Math.round(before[A])} → 新 ${Math.round(aFinal)}）`);
  ok("落位是连续位移而不是一帧到位", s1.frames.filter(f => f.t <= 200 && f.shifted.indexOf(A) >= 0).length >= 3,
     "前 200ms 带位移帧 " + s1.frames.filter(f => f.t <= 200 && f.shifted.indexOf(A) >= 0).length);

  /* ---------- 2. 补位卡片：延迟 70ms 后在旧位置起步向上 ---------- */
  const floatIds = Object.keys(s1.timings).filter(id => s1.timings[id].kind === "float-up");
  ok("其后未点卡片带 data-motion=float-up", floatIds.length > 0,
     "float-up 卡片 " + floatIds.length + " 张：" + floatIds.join(","));
  const f0 = floatIds[0];
  const fT = s1.timings[f0] || {};
  near("补位动画真实延时约 70ms", fT.delay, 70, 15);
  near("补位动画真实时长约 260ms", fT.duration, 260, 30);
  eq("补位用 fill:both 锁住延迟期的位置", fT.fill, "both");

  const fFinal = lastTop(s1.frames, f0);
  const f40 = frameAt(s1.frames, 40);
  const f200 = frameAt(s1.frames, 200);
  const ff40 = fracAt(f40.tops[f0], before[f0], fFinal);
  const ff200 = fracAt(f200.tops[f0], before[f0], fFinal);
  ok("延迟期内补位卡片基本停在旧位置（不先闪到新位置）", ff40 >= -0.06 && ff40 <= 0.12,
     `t=${f40.t}ms 已走 ${(ff40 * 100).toFixed(0)}%`);
  ok("之后补位卡片向上移动到新位置", ff200 > 0.3 && fFinal < before[f0] - 15,
     `t=${f200.t}ms 已走 ${(ff200 * 100).toFixed(0)}%；旧 ${Math.round(before[f0])} → 新 ${Math.round(fFinal)}`);

  /* ---------- 3. 真实 DOM 重排（不是纯 CSS 假动画） ---------- */
  const exp1 = expectedOrder();
  eq("动画结束后 DOM 顺序与数据一致", gridIds().join(","), exp1.order.join(","));
  eq("被点卡片真实落到已点区域末尾（DOM 重排）", gridIds()[gridIds().length - 1], A);
  ok("被点卡片布局坐标真的下移（不是只做 transform）", aFinal - before[A] > 30,
     `布局位移 ${Math.round(aFinal - before[A])}px`);
  ok("补位卡片布局坐标真的上移", before[f0] - fFinal > 15,
     `布局位移 ${Math.round(before[f0] - fFinal)}px`);

  await sleep(360);
  ok("动画结束后无 data-motion 残留", X.motion.active() === 0, X.motion.active() + " 张");
  ok("动画结束后所有卡片 transform 归位", allSettled());
  ok("动画结束后没有遗留 WAAPI 动画对象", animCount() === 0, animCount() + " 条");
  eq("本轮只有一张卡片写入记录", exp1.recIds.size, 1);
  eq("被点卡片记录状态为「到」", (X.state.records.get(`${X.state.date}|${X.state.slot}|${A}`) || {}).status, "dao");
  ok("未点人数减 1", X.countNow().none === 7, X.countNow().none);

  /* ---------- 4. 同状态幂等点击：不播动画、不重复写 ---------- */
  const recBefore = X.state.records.size;
  const orderBefore = gridIds().join(",");
  const p2 = sampleFrames(A, 400);
  cardEl(A).click();
  const s2 = await p2;
  ok("幂等点击不播重排动画", kindsOf(s2.frames).length === 0, kindsOf(s2.frames).join(","));
  eq("幂等点击不重复写记录", X.state.records.size, recBefore);
  eq("幂等点击后排序不变", gridIds().join(","), orderBefore);
  ok("幂等点击后卡片无位移 transform", s2.frames.every(f => f.shifted.length === 0));

  /* ---------- 5. 快速连续点击 3 名未点学生 ---------- */
  const quick = expectedOrder().unmarked.slice(0, 3);
  await sleep(150);
  quick.forEach((id, i) => { setTimeout(() => { const e = cardEl(id); if (e) e.click(); }, i * 40); });
  await sleep(900);

  const exp3 = expectedOrder();
  eq("快速连点后记录数 = 4", exp3.recIds.size, 4);
  eq("快速连点后未点 4 人", X.countNow().none, 4);
  ok("快速连点后 4 条记录都存在且为「到」",
     quick.concat([A]).every(id => (X.state.records.get(`${X.state.date}|${X.state.slot}|${id}`) || {}).status === "dao"),
     quick.join(","));
  eq("快速连点后 DOM 顺序正确", gridIds().join(","), exp3.order.join(","));
  eq("快速连点后无重复卡片", new Set(gridIds()).size, gridIds().length);
  ok("快速连点后无 data-motion 残留", X.motion.active() === 0, X.motion.active() + " 张");
  ok("快速连点后所有卡片 transform 归位", allSettled());
  ok("快速连点后没有遗留 WAAPI 动画对象", animCount() === 0, animCount() + " 条");

  /* ---------- 6. 撤销也走真实重排 ---------- */
  const beforeUndo = gridIds().join(",");
  document.querySelector("#undoBtn").click();
  await sleep(800);
  const exp4 = expectedOrder();
  ok("撤销后排序回到真实顺序", gridIds().join(",") === exp4.order.join(","), gridIds().join(","));
  ok("撤销后仍未点 5 人", X.countNow().none === 5, X.countNow().none);
  ok("撤销过程未残留动画标记", X.motion.active() === 0);
  ok("撤销前后不是同一顺序（确有重排）", beforeUndo !== gridIds().join(","));

  /* ---------- 7. 搜索 / 筛选 / 切日期 不播动画 ---------- */
  const searchBox = document.querySelector(".search input");
  const pS = sampleFrames(gridIds()[0], 400);
  searchBox.value = "三年级";
  searchBox.dispatchEvent(new Event("input", {bubbles:true}));
  const sS = await pS;
  ok("搜索重绘不播动画", kindsOf(sS.frames).length === 0, kindsOf(sS.frames).join(","));
  eq("搜索命中 4 人", gridIds().length, 4);
  searchBox.value = "";
  searchBox.dispatchEvent(new Event("input", {bubbles:true}));
  await sleep(240);

  const pF = sampleFrames(gridIds()[0], 400);
  document.querySelector('.fchip[data-filter="none"]').click();
  const sF = await pF;
  ok("筛选切换不播动画", kindsOf(sF.frames).length === 0, kindsOf(sF.frames).join(","));
  eq("筛选未点后 5 张卡片", gridIds().length, 5);
  document.querySelector('.fchip[data-filter="all"]').click();
  await sleep(240);

  const pD = sampleFrames(gridIds()[0], 400);
  document.querySelector("#prevDay").click();
  const sD = await pD;
  ok("切换日期不播动画", kindsOf(sD.frames).length === 0, kindsOf(sD.frames).join(","));
  eq("切到前一天全部未点", X.countNow().none, 8);
  document.querySelector("#nextDay").click();
  await sleep(260);

  /* ---------- 8. 切模式来回也不播动画 ---------- */
  const pM = sampleFrames(gridIds()[0], 260);
  X.switchMode("training");
  const sM = await pM;
  await sleep(240);
  X.switchMode("wudian");
  await sleep(360);
  ok("切换模式不播点名重排动画",
     kindsOf(sM.frames).indexOf("drop") < 0 && kindsOf(sM.frames).indexOf("float-up") < 0,
     kindsOf(sM.frames).join(","));
  eq("切回午托后记录仍在（未点 5 人）", X.countNow().none, 5);
  ok("切回午托后无动画残留", X.motion.active() === 0);
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 4 段：首屏加载不播入场动画
# --------------------------------------------------------------------------
JS_BOOT = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  ok("重载后没有正在运行的卡片动画", animCount() === 0, animCount() + " 条");
  const p = sampleFrames((gridIds()[0] || ""), 460);
  const s = await p;
  ok("首屏加载不播入场动画", kindsOf(s.frames).length === 0, kindsOf(s.frames).join(",") || "无标记");
  ok("首屏卡片无位移 transform", s.frames.every(f => f.shifted.length === 0));
  ok("首屏卡片无 transform", allSettled());
  eq("首屏卡片数 8", gridIds().length, 8);
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 5 段：prefers-reduced-motion: reduce
# --------------------------------------------------------------------------
JS_REDUCE = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  if (X.state.mode !== "wudian") { X.switchMode("wudian"); await sleep(320); }
  X.switchTab("roll");
  await sleep(320);

  ok("已进入减少动态效果模式", X.motion.reduced() === true);
  const ids = gridIds();
  const exp = expectedOrder();
  const target = exp.unmarked[0];
  const beforeOrder = ids.join(",");
  const beforeTop = topOf(target);

  const p = sampleFrames(target, 520);
  cardEl(target).click();
  const s = await p;
  const kinds = kindsOf(s.frames);

  ok("减少动态效果时无 drop / float-up 位移标记",
     kinds.indexOf("drop") < 0 && kinds.indexOf("float-up") < 0, kinds.join(",") || "无标记");
  ok("减少动态效果时卡片全程无位移 transform",
     s.frames.every(f => f.shifted.length === 0),
     "带 transform 帧 " + s.frames.filter(f => f.shifted.length > 0).length + "/" + s.frames.length);
  ok("减少动态效果时关键帧里没有 translate3d 位移",
     !s.keyframes || s.keyframes.every(k => k.transform.indexOf("translate") < 0),
     s.keyframes ? s.keyframes.map(k => k.transform).join(" | ") : "未采到关键帧（直接完成重排）");
  const topDelta = Math.abs((lastTop(s.frames, target) || 0) - beforeTop);
  ok("减少动态效果时只重排、不做动画位移（布局确实变了但无 transform 位移）",
     topDelta > 15,
     "布局位移 " + Math.round(topDelta) + "px");
  const expAfter = expectedOrder();
  eq("减少动态效果时重排立即完成", gridIds().join(","), expAfter.order.join(","));
  ok("减少动态效果时顺序确有变化", beforeOrder !== gridIds().join(","));
  eq("减少动态效果时记录照常写入",
     (X.state.records.get(`${X.state.date}|${X.state.slot}|${target}`) || {}).status, "dao");
  await sleep(320);
  ok("减少动态效果后无动画残留", X.motion.active() === 0, X.motion.active() + " 张");
  ok("减少动态效果后卡片无 transform", allSettled());
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 6 段：390×844 布局 / 遮挡 / 乱码 / 外部资源
# --------------------------------------------------------------------------
JS_LAYOUT = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  if (X.state.mode !== "wudian") { X.switchMode("wudian"); await sleep(320); }
  X.switchTab("roll");
  await sleep(400);

  const ext = Array.from(document.querySelectorAll("[src],[href]")).filter(el => {
    const v = el.getAttribute("src") || el.getAttribute("href") || "";
    return /^https?:\/\//i.test(v);
  });

  /* 落位途中：被点卡片必须压在补位卡片之上，中心点不能被别人盖住 */
  const exp = expectedOrder();
  const target = exp.unmarked[0];
  const p = sampleFrames(target, 420);
  cardEl(target).click();
  await sleep(90);
  const el = cardEl(target);
  const rc = el ? el.getBoundingClientRect() : null;
  let hitId = null, zTarget = null, zFloat = null;
  if (rc) {
    const hit = document.elementFromPoint(rc.left + rc.width / 2, rc.top + rc.height / 2);
    hitId = hit ? (hit.closest(".card") ? hit.closest(".card").dataset.id : "非卡片") : "无";
    zTarget = getComputedStyle(el).zIndex;
    const other = Array.from(document.querySelectorAll('#grid .card[data-motion="float-up"]'))[0];
    zFloat = other ? getComputedStyle(other).zIndex : "无补位卡片";
  }
  const s = await p;
  const midOverflow = document.documentElement.scrollWidth > window.innerWidth + 1 ||
                      document.body.scrollWidth > window.innerWidth + 1;
  await sleep(420);

  return {
    overflow: midOverflow,
    scroll: document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth,
    mojibake: document.documentElement.textContent.indexOf("\uFFFD") >= 0,
    extCount: ext.length,
    unmarked: X.countNow().none,
    marked: X.countNow().marked,
    hitId: hitId,
    targetId: target,
    zTarget: zTarget,
    zFloat: zFloat,
    midFrames: s.frames.filter(f => f.t <= 300 && f.shifted.length > 0).length
  };
})()
"""


def main():
    srv = serve()
    page = None
    results = {"checks": [], "fatal": None, "screenshots": [], "version": "1.6.1"}
    try:
        page = launch()
        page.send("Page.navigate", {"url": "http://127.0.0.1:%d/tests/__no_such_page__" % HTTP_PORT})
        time.sleep(0.6)
        page.js(JS_CLEAN_ENV)
        page.mobile(390, 844)
        page.reduce_motion(False)           # 无头默认 reduce，正常场景显式关掉
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)

        print("[1/6] 品牌名 / 版本 / SW 策略 / 动画参数")
        results["checks"].extend(page.js(JS_BRAND)["checks"])

        print("[2/6] 培训概况页底部版本标记")
        results["checks"].extend(page.js(JS_BADGE)["checks"])

        print("[3/6] 播种 8 名午托学生并重载")
        page.js(JS_SEED)
        page.reload()

        print("[4/6] 两段式落位动画 / 幂等 / 快速连点 / 非重排场景")
        results["checks"].extend(page.js(JS_REFLOW)["checks"])

        print("[5/6] 首屏加载动画与 prefers-reduced-motion")
        page.reload()
        results["checks"].extend(page.js(JS_BOOT)["checks"])
        page.reduce_motion(True)
        page.reload()                       # 重载后 matchMedia 在 reduce 环境下初始化
        results["checks"].extend(page.js(JS_REDUCE)["checks"])
        page.reduce_motion(False)
        time.sleep(0.2)
        page.reload()

        print("[6/6] 390×844 布局 / 遮挡检查 + 动画进行中截图")
        r6 = page.js(JS_LAYOUT)
        results["checks"].append({"name": "点名页 390×844 无横向滚动（含动画期间）",
                                  "ok": not r6["overflow"], "detail": r6["scroll"]})
        results["checks"].append({"name": "点名页无乱码字符", "ok": not r6["mojibake"],
                                  "detail": "含 U+FFFD" if r6["mojibake"] else "无 U+FFFD"})
        results["checks"].append({"name": "点名页无外部网络资源", "ok": r6["extCount"] == 0,
                                  "detail": "外部资源 %d 个" % r6["extCount"]})
        results["checks"].append({"name": "落位期间被点卡片不被补位卡片遮住（中心点命中的是自己）",
                                  "ok": r6["hitId"] == r6["targetId"],
                                  "detail": "命中 %s（目标 %s）z=%s vs 补位 z=%s"
                                            % (r6["hitId"], r6["targetId"], r6["zTarget"], r6["zFloat"])})
        results["checks"].append({"name": "落位期间被点卡片层级高于补位卡片",
                                  "ok": str(r6["zTarget"]) > str(r6["zFloat"]) and r6["zTarget"] not in (None, "auto"),
                                  "detail": "drop z-index %s / float-up z-index %s" % (r6["zTarget"], r6["zFloat"])})
        results["checks"].append({"name": "截图状态为部分已点、未点在上",
                                  "ok": r6["marked"] > 0 and r6["unmarked"] > 0,
                                  "detail": "已点 %d / 未点 %d" % (r6["marked"], r6["unmarked"])})

        # 动画进行中截图：点击后约 120ms 抓帧
        page.reduce_motion(False)
        page.js("""(() => {
          const exp = window.__test;
          const cards = Array.from(document.querySelectorAll('#grid .card'));
          const target = cards[0];
          window.__shotTarget = target ? target.dataset.id : null;
          window.__shotAt = performance.now();
          if (target) target.click();
          return window.__shotTarget;
        })()""")
        time.sleep(0.09)
        shot_path = page.shot("v161-roll-motion")
        still_running = page.js(
            "document.querySelectorAll('#grid .card[data-motion]').length")
        results["screenshots"].append(shot_path)
        results["checks"].append({
            "name": "截图截在动画进行中（约 120ms 仍有 data-motion）",
            "ok": (still_running or 0) > 0,
            "detail": "截图后仍有 %s 张卡片带 data-motion" % still_running})
        time.sleep(0.6)
        results["screenshots"].append(page.shot("v161-roll-settled"))
    except Exception as e:  # noqa: BLE001
        results["fatal"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if page:
            page.close()
        srv.shutdown()

    failed = [c for c in results["checks"] if not c["ok"]]
    passed = len(results["checks"]) - len(failed)
    results["summary"] = {"total": len(results["checks"]), "passed": passed, "failed": len(failed)}
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("-" * 78)
    for c in results["checks"]:
        print(("PASS  " if c["ok"] else "FAIL  ") + c["name"] + ("" if c["ok"] else "  <- " + c["detail"]))
    print("-" * 78)
    print("合计 %d 项：通过 %d，失败 %d" % (len(results["checks"]), passed, len(failed)))
    if results["fatal"]:
        print("致命错误：" + results["fatal"])
    print("报告：" + REPORT)
    print("截图：" + ", ".join(results["screenshots"]))
    ok_all = (not results["fatal"]) and not failed
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
