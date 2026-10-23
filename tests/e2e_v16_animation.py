# -*- coding: utf-8 -*-
"""午托点名 v1.6 —— 手机桌面品牌名 + 点名重排动画（FLIP / WAAPI）端到端自测

真机（无头 Edge + CDP，390×844）验证：
  1. manifest 的 name / short_name 与 apple-mobile-web-app-title 精确为「宇才教育」，
     页面业务标题仍是「午托点名」
  2. 版本 1.6.0、Service Worker 缓存版本 wudian-v1.6.0、DB_VER 仍为 3（不迁移）
  3. 点击未点学生后：被点卡片早期位于最终位置上方（向下落位，data-motion=drop），
     其后的未点卡片早期位于下方（向上补位，data-motion=float-up）
  4. 动画结束后：无 data-motion 残留、transform 归零、DOM 顺序与数据状态一致
  5. 同状态幂等点击不播重排动画、不重复写记录
  6. 快速连续点击多名学生：数据不丢、无重复卡片、最终排序正确
  7. 首屏加载 / 搜索 / 筛选 / 切换日期 / 切换模式一律不播入场或重排动画
  8. prefers-reduced-motion: reduce 时无上下位移，只保留 ≤150ms 轻反馈
  9. 390×844 无横向滚动、无乱码、无外部资源；保留点名页最终截图

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
SHOT_DIR = os.path.join(ROOT, "screenshots")        # v1.6 交付截图放根 screenshots/
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
const sleep = ms => new Promise(r => setTimeout(r, ms));
const gridIds = () => Array.from(document.querySelectorAll("#grid .card")).map(c => c.dataset.id);
const cardEl = id => document.querySelector(`#grid .card[data-id="${id}"]`);
const settled = el => {
  const t = getComputedStyle(el).transform;
  return t === "none" || /^matrix\(1, 0, 0, 1, 0, 0\)$/.test(t) ||
         /^matrix3d\(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1\)$/.test(t);
};
/* 逐帧采样：所有卡片 top / data-motion / 是否带位移 transform / 目标卡片动画关键帧 */
function sampleFrames(targetId, ms) {
  const t0 = performance.now();
  const frames = [];
  let keyframe = null;
  let finished = false;
  return new Promise(res => {
    const finish = () => {
      if (finished) return;
      finished = true;
      res({frames, keyframe});
    };
    setTimeout(finish, ms + 900);          /* rAF 被节流时的兜底 */
    function tick() {
      if (finished) return;
      const cards = Array.from(document.querySelectorAll("#grid .card"));
      const item = {t: Math.round(performance.now() - t0), tops: {}, motions: {}, shifted: []};
      cards.forEach(c => {
        item.tops[c.dataset.id] = Math.round(c.getBoundingClientRect().top * 100) / 100;
        const m = c.getAttribute("data-motion");
        if (m) item.motions[c.dataset.id] = m;
        const tf = getComputedStyle(c).transform;
        if (tf && tf !== "none" && tf !== "matrix(1, 0, 0, 1, 0, 0)" &&
            tf !== "matrix3d(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)") {
          item.shifted.push(c.dataset.id);
        }
      });
      const tgt = cardEl(targetId);
      if (tgt && tgt.getAnimations && !keyframe) {
        const as = tgt.getAnimations();
        if (as.length && as[0].effect) {
          keyframe = as[0].effect.getKeyframes().map(k => String(k.transform || ""));
        }
      }
      frames.push(item);
      if (performance.now() - t0 < ms) requestAnimationFrame(tick);
      else finish();
    }
    requestAnimationFrame(tick);
  });
}
const kindsOf = frames => {
  const s = new Set();
  frames.forEach(f => Object.values(f.motions).forEach(m => s.add(m)));
  return Array.from(s);
};
const firstSeen = (frames, id, kind) => frames.find(f => f.motions[id] === kind) || null;
const lastTop = (frames, id) => frames.length ? frames[frames.length - 1].tops[id] : null;
const topDelta = (frames, id) => {
  const all = frames.map(f => f.tops[id]).filter(v => v != null);
  return all.length ? Math.max.apply(null, all) - Math.min.apply(null, all) : 0;
};
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
# 第 1 段：品牌名 / 版本 / 不清库
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
  eq("APP_VERSION 为 1.6.0", X.appVersion, "1.6.0");

  const sw = await fetch("sw.js", {cache:"no-store"}).then(r => r.text());
  ok("Service Worker 版本升到 wudian-v1.6.0", sw.indexOf('"wudian-v1.6.0"') >= 0,
     (sw.match(/wudian-v[\d.]+/) || ["未找到"])[0]);
  ok("缓存壳版本号与 APP 版本一致（无旧 v1.5.0 残留）", sw.indexOf("wudian-v1.5.0") < 0);

  const db = await new Promise((res, rej) => {
    const r = indexedDB.open("wudian-db"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  eq("IndexedDB 版本仍为 3（本轮不迁移）", db.version, 3);
  ok("八张业务表齐全（午托 4 + 培训 5）",
     ["students","records","avatars","kv","trTeachers","trStudents","trSubjects","trCourses","trAttendance"]
       .every(s => Array.from(db.objectStoreNames).indexOf(s) >= 0),
     Array.from(db.objectStoreNames).join(","));

  ok("重排动画参数在 180-220ms 之间", X.motion.duration >= 180 && X.motion.duration <= 220, X.motion.duration + "ms");
  eq("动画曲线为 cubic-bezier(0.23, 1, 0.32, 1)", X.motion.easing, "cubic-bezier(0.23, 1, 0.32, 1)");
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
# 第 2 段：点击重排动画 / 幂等 / 快速连点 / 非重排场景不播
# --------------------------------------------------------------------------
JS_REFLOW = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  if (X.state.mode !== "wudian") { X.switchMode("wudian"); await sleep(320); }
  X.switchTab("roll");
  await sleep(320);

  const ids0 = gridIds();
  eq("播种后点名页 8 张卡片", ids0.length, 8);
  eq("初始全部未点", X.countNow().none, 8);
  ok("初始没有动画标记", X.motion.active() === 0);
  ok("初始卡片无 transform",
     Array.from(document.querySelectorAll("#grid .card")).every(settled));

  /* ---------- 1. 点击第一张未点卡片：真实重排播动画 ---------- */
  const A = ids0[0], B = ids0[1], C = ids0[2];
  const p1 = sampleFrames(A, 520);
  cardEl(A).click();
  const s1 = await p1;
  const kinds1 = kindsOf(s1.frames);

  const dropFrame = firstSeen(s1.frames, A, "drop");
  ok("被点卡片带 data-motion=drop", !!dropFrame, kinds1.join(",") || "无标记");
  const floatIds = Object.keys(s1.frames.reduce((acc, f) => {
    Object.keys(f.motions).forEach(id => { if (f.motions[id] === "float-up") acc[id] = 1; });
    return acc;
  }, {}));
  ok("其后未点卡片带 data-motion=float-up", floatIds.length > 0,
     "float-up 卡片 " + floatIds.length + " 张：" + floatIds.join(","));

  const aFinal = lastTop(s1.frames, A);
  ok("被点卡片早期在最终位置上方（向下落位观感）",
     !!dropFrame && dropFrame.tops[A] < aFinal - 15,
     dropFrame ? ("早期 " + dropFrame.tops[A] + " → 终态 " + aFinal) : "未采到 drop 帧");
  ok("drop 关键帧从负向 Y 位移归零",
     !!s1.keyframe && /translate3d\(-?\d+(\.\d+)?px,\s*-\d+/.test(s1.keyframe[0]),
     s1.keyframe ? s1.keyframe.join(" | ") : "未采到关键帧");

  const floatOk = floatIds.map(id => {
    const f = firstSeen(s1.frames, id, "float-up");
    const fin = lastTop(s1.frames, id);
    return {id, early: f ? f.tops[id] : null, fin, ok: !!f && f.tops[id] > fin + 15};
  });
  ok("补位卡片早期在下方（向上漂浮观感）", floatOk.length > 0 && floatOk.every(x => x.ok),
     floatOk.map(x => x.id + " " + x.early + "→" + x.fin).join("; "));
  ok("C 卡片（跨行补位）也在 float-up 名单内", floatOk.some(x => x.id === C),
     floatOk.map(x => x.id).join(","));
  ok("动画期间卡片确实带有位移 transform", s1.frames.some(f => f.shifted.length > 0),
     "带 transform 帧 " + s1.frames.filter(f => f.shifted.length > 0).length + "/" + s1.frames.length);

  await sleep(260);   /* 等动画与 CSS transition 彻底结束 */
  ok("动画结束后无 data-motion 残留", X.motion.active() === 0, X.motion.active() + " 张");
  ok("动画结束后所有卡片 transform 归位",
     Array.from(document.querySelectorAll("#grid .card")).every(settled));

  const exp1 = expectedOrder();
  eq("动画结束后 DOM 顺序与数据一致", gridIds().join(","), exp1.order.join(","));
  eq("被点卡片落到已点区域末尾", gridIds()[gridIds().length - 1], A);
  eq("本轮只有一张卡片写入记录", exp1.recIds.size, 1);
  eq("被点卡片记录状态为「到」", (X.state.records.get(`${X.state.date}|${X.state.slot}|${A}`) || {}).status, "dao");
  ok("未点人数减 1", X.countNow().none === 7, X.countNow().none);

  /* ---------- 2. 同状态幂等点击：不播动画、不重复写 ---------- */
  const recBefore = X.state.records.size;
  const orderBefore = gridIds().join(",");
  const p2 = sampleFrames(A, 300);
  cardEl(A).click();
  const s2 = await p2;
  ok("幂等点击不播重排动画", kindsOf(s2.frames).length === 0, kindsOf(s2.frames).join(","));
  eq("幂等点击不重复写记录", X.state.records.size, recBefore);
  eq("幂等点击后排序不变", gridIds().join(","), orderBefore);
  ok("幂等点击后卡片无位移 transform",
     s2.frames.every(f => f.shifted.length === 0));

  /* ---------- 3. 快速连续点击 3 名未点学生 ---------- */
  const quick = expectedOrder().unmarked.slice(0, 3);
  await sleep(120);
  quick.forEach((id, i) => { setTimeout(() => { const e = cardEl(id); if (e) e.click(); }, i * 40); });
  await sleep(700);

  const exp3 = expectedOrder();
  eq("快速连点后记录数 = 4", exp3.recIds.size, 4);
  eq("快速连点后未点 4 人", X.countNow().none, 4);
  ok("快速连点后 4 条记录都存在且为「到」",
     quick.concat([A]).every(id => (X.state.records.get(`${X.state.date}|${X.state.slot}|${id}`) || {}).status === "dao"),
     quick.join(","));
  eq("快速连点后 DOM 顺序正确", gridIds().join(","), exp3.order.join(","));
  eq("快速连点后无重复卡片", new Set(gridIds()).size, gridIds().length);
  ok("快速连点后无 data-motion 残留", X.motion.active() === 0, X.motion.active() + " 张");
  ok("快速连点后所有卡片 transform 归位",
     Array.from(document.querySelectorAll("#grid .card")).every(settled));

  /* ---------- 4. 撤销也走真实重排（同样只动位置，不改数据口径） ---------- */
  const beforeUndo = gridIds().join(",");
  document.querySelector("#undoBtn").click();
  await sleep(600);
  const exp4 = expectedOrder();
  ok("撤销后排序回到真实顺序", gridIds().join(",") === exp4.order.join(","), gridIds().join(","));
  ok("撤销后仍未点 5 人", X.countNow().none === 5, X.countNow().none);
  ok("撤销过程未残留动画标记", X.motion.active() === 0);
  ok("撤销前后不是同一顺序（确有重排）", beforeUndo !== gridIds().join(","));

  /* ---------- 5. 搜索 / 筛选 / 切日期 不播动画 ---------- */
  const searchBox = document.querySelector(".search input");
  const pS = sampleFrames(gridIds()[0], 320);
  searchBox.value = "三年级";
  searchBox.dispatchEvent(new Event("input", {bubbles:true}));
  const sS = await pS;
  ok("搜索重绘不播动画", kindsOf(sS.frames).length === 0, kindsOf(sS.frames).join(","));
  eq("搜索命中 4 人", gridIds().length, 4);
  searchBox.value = "";
  searchBox.dispatchEvent(new Event("input", {bubbles:true}));
  await sleep(200);

  const pF = sampleFrames(gridIds()[0], 320);
  document.querySelector('.fchip[data-filter="none"]').click();
  const sF = await pF;
  ok("筛选切换不播动画", kindsOf(sF.frames).length === 0, kindsOf(sF.frames).join(","));
  eq("筛选未点后 5 张卡片", gridIds().length, 5);
  document.querySelector('.fchip[data-filter="all"]').click();
  await sleep(200);

  const pD = sampleFrames(gridIds()[0], 320);
  document.querySelector("#prevDay").click();
  const sD = await pD;
  ok("切换日期不播动画", kindsOf(sD.frames).length === 0, kindsOf(sD.frames).join(","));
  eq("切到前一天全部未点", X.countNow().none, 8);
  document.querySelector("#nextDay").click();
  await sleep(220);

  /* ---------- 6. 切模式来回也不播动画 ---------- */
  const pM = sampleFrames(gridIds()[0], 200);
  X.switchMode("training");
  const sM = await pM;
  await sleep(200);
  X.switchMode("wudian");
  await sleep(320);
  ok("切换模式不播点名重排动画", kindsOf(sM.frames).indexOf("drop") < 0 &&
     kindsOf(sM.frames).indexOf("float-up") < 0, kindsOf(sM.frames).join(","));
  eq("切回午托后记录仍在（未点 5 人）", X.countNow().none, 5);
  ok("切回午托后无动画残留", X.motion.active() === 0);

  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 3 段：首屏加载不播入场动画
# --------------------------------------------------------------------------
JS_BOOT = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  ok("重载后没有正在运行的卡片动画",
     Array.from(document.querySelectorAll("#grid .card"))
       .every(c => !c.getAnimations || c.getAnimations().length === 0));
  const p = sampleFrames((gridIds()[0] || ""), 420);
  const s = await p;
  ok("首屏加载不播入场动画", kindsOf(s.frames).length === 0, kindsOf(s.frames).join(",") || "无标记");
  ok("首屏卡片无位移 transform", s.frames.every(f => f.shifted.length === 0));
  ok("首屏卡片无 transform",
     Array.from(document.querySelectorAll("#grid .card")).every(settled));
  eq("首屏卡片数 8", gridIds().length, 8);
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 4 段：prefers-reduced-motion: reduce
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

  const p = sampleFrames(target, 460);
  cardEl(target).click();
  const s = await p;
  const kinds = kindsOf(s.frames);

  ok("减少动态效果时无 drop / float-up 位移标记",
     kinds.indexOf("drop") < 0 && kinds.indexOf("float-up") < 0, kinds.join(",") || "无标记");
  ok("减少动态效果时卡片全程无位移 transform",
     s.frames.every(f => f.shifted.length === 0),
     "带 transform 帧 " + s.frames.filter(f => f.shifted.length > 0).length + "/" + s.frames.length);
  ok("减少动态效果时关键帧里没有 translate3d 位移",
     !s.keyframe || s.keyframe.every(k => k.indexOf("translate") < 0),
     s.keyframe ? s.keyframe.join(" | ") : "未采到关键帧（直接完成重排）");
  const expAfter = expectedOrder();
  eq("减少动态效果时重排立即完成", gridIds().join(","), expAfter.order.join(","));
  ok("减少动态效果时顺序确有变化", beforeOrder !== gridIds().join(","));
  eq("减少动态效果时记录照常写入",
     (X.state.records.get(`${X.state.date}|${X.state.slot}|${target}`) || {}).status, "dao");
  await sleep(260);
  ok("减少动态效果后无动画残留", X.motion.active() === 0, X.motion.active() + " 张");
  ok("减少动态效果后卡片无 transform",
     Array.from(document.querySelectorAll("#grid .card")).every(settled));
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 5 段：390×844 布局 / 乱码 / 外部资源
# --------------------------------------------------------------------------
JS_LAYOUT = r"""
(async () => {
""" + JS_HELPERS + r"""
  await X.training.ready();
  if (X.state.mode !== "wudian") { X.switchMode("wudian"); await sleep(320); }
  X.switchTab("roll");
  await sleep(360);
  const ext = Array.from(document.querySelectorAll("[src],[href]")).filter(el => {
    const v = el.getAttribute("src") || el.getAttribute("href") || "";
    return /^https?:\/\//i.test(v);
  });
  return {
    overflow: document.documentElement.scrollWidth > window.innerWidth + 1 ||
              document.body.scrollWidth > window.innerWidth + 1,
    scroll: document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth,
    mojibake: document.documentElement.textContent.indexOf("\uFFFD") >= 0,
    extCount: ext.length,
    unmarked: X.countNow().none,
    marked: X.countNow().marked
  };
})()
"""


def main():
    srv = serve()
    page = None
    results = {"checks": [], "fatal": None, "screenshots": [], "version": "1.6.0"}
    try:
        page = launch()
        page.send("Page.navigate", {"url": "http://127.0.0.1:%d/tests/__no_such_page__" % HTTP_PORT})
        time.sleep(0.6)
        page.js(JS_CLEAN_ENV)
        page.mobile(390, 844)
        page.reduce_motion(False)           # 无头默认 reduce，正常场景显式关掉
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)

        print("[1/5] 品牌名 / 版本 / 数据库版本")
        results["checks"].extend(page.js(JS_BRAND)["checks"])

        print("[2/5] 播种 8 名午托学生并重载")
        page.js(JS_SEED)
        page.reload()

        print("[3/5] 点击重排动画 / 幂等 / 快速连点 / 非重排场景")
        results["checks"].extend(page.js(JS_REFLOW)["checks"])

        print("[4/5] 首屏加载动画与 prefers-reduced-motion")
        page.reload()
        results["checks"].extend(page.js(JS_BOOT)["checks"])
        page.reduce_motion(True)
        page.reload()                       # 重载后 matchMedia 在 reduce 环境下初始化
        results["checks"].extend(page.js(JS_REDUCE)["checks"])
        page.reduce_motion(False)
        time.sleep(0.2)
        page.reload()

        print("[5/5] 390×844 布局检查 + 截图")
        r5 = page.js(JS_LAYOUT)
        results["checks"].append({"name": "点名页 390×844 无横向滚动",
                                  "ok": not r5["overflow"], "detail": r5["scroll"]})
        results["checks"].append({"name": "点名页无乱码字符", "ok": not r5["mojibake"],
                                  "detail": "含 U+FFFD" if r5["mojibake"] else "无 U+FFFD"})
        results["checks"].append({"name": "点名页无外部网络资源", "ok": r5["extCount"] == 0,
                                  "detail": "外部资源 %d 个" % r5["extCount"]})
        results["checks"].append({"name": "截图状态为部分已点、未点在上",
                                  "ok": r5["marked"] > 0 and r5["unmarked"] > 0,
                                  "detail": "已点 %d / 未点 %d" % (r5["marked"], r5["unmarked"])})
        page.reduce_motion(False)
        results["screenshots"].append(page.shot("v16-roll-motion"))
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

    print("-" * 70)
    for c in results["checks"]:
        print(("PASS  " if c["ok"] else "FAIL  ") + c["name"] + ("" if c["ok"] else "  <- " + c["detail"]))
    print("-" * 70)
    print("合计 %d 项：通过 %d，失败 %d" % (len(results["checks"]), passed, len(failed)))
    if results["fatal"]:
        print("致命错误：" + results["fatal"])
    print("报告：" + REPORT)
    print("截图：" + ", ".join(results["screenshots"]))
    ok_all = (not results["fatal"]) and not failed
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
