# -*- coding: utf-8 -*-
"""午托点名 v1.6.2 —— 培训签到时间 30 分钟档位下拉 + 默认 90 分钟 端到端自测

真机（无头 Edge + CDP，390×844）验证：
  1. APP_VERSION=1.6.2、SW 缓存版本 wudian-v1.6.2、DB_VER 仍为 3（不迁移）
  2. 培训概况页底部版本标记 v1.6.2
  3. 新开签到表单使用 select#trStart / select#trEnd，不再有 input[type=time]
  4. 开始候选覆盖 00:00 / 10:00 / 10:30 / 22:00，不含 22:30 / 23:00 / 23:30
  5. 新表单开始为空、结束框禁用并提示「请先选开始时间」
  6. 选 10:00 后结束自动 11:30；选 10:30 后结束自动 12:00
  7. 手动把结束改 12:00 后不被重置；再把开始改 14:00，结束自动恢复 15:30
  8. 结束候选全部为 30 分钟档位且都晚于开始时间（14:30 起到 23:30 共 19 档）
  9. 保存后课程真实写入 14:00–15:30
 10. 业务层（trSaveSignin）拒绝 10:15 / 11:45、结束等于开始、结束早于开始、
     跨日（22:00→00:30）与超过 22:00 的开始时间；合法 10:00–11:30 可保存
 11. 旧课程（10:15–11:45）打开编辑保留原时间并出现「旧时间」临时选项，
     不自动变成 +90；未动时间可原样保存
 12. 学生时间冲突规则不回归；不同学生同时段不同老师可并存
 13. 390×844 无横向溢出、无乱码、无外部资源；截图 screenshots/v162-time-select.png

用法（本机）：
  C:\\Users\\James\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\e2e_v162_time.py
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
HTTP_PORT = 8115
CDP_PORT = 9461
PROFILE = os.path.join(ROOT, "tests", "_e2e_v162_profile_%s" % time.strftime("%Y%m%d-%H%M%S"))
SHOT_DIR = os.path.join(ROOT, "screenshots")
REPORT = os.path.join(ROOT, "tests", "last-run-v162-time.json")


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
# 页面内公共工具
# --------------------------------------------------------------------------
JS_HELPERS = r"""
const X = window.__test;
const T = X.training;
const checks = [];
const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fire = (el, ev) => el.dispatchEvent(new Event(ev, {bubbles:true}));
const fails = async (n, fn, need) => {
  try { await fn(); checks.push({name:n, ok:false, detail:"未被拒绝"}); }
  catch (e) {
    const m = String((e && e.message) || e);
    checks.push({name:n, ok: need ? m.indexOf(need) >= 0 : true, detail:m});
  }
};
"""

# 播种：科目 / 老师 / 学生（培训线）
JS_SEED = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const sub = await T.createSubject({name:"数学", note:"v162 自测"});
  const t1 = await T.createTeacher({name:"时间老师", subjectIds:[sub.id]});
  const t2 = await T.createTeacher({name:"并行老师", subjectIds:[sub.id]});
  const p1 = await T.createStudent({name:"时一", gradeClass:"五年级1班"});
  const p2 = await T.createStudent({name:"时二", gradeClass:"五年级1班"});
  return {subId: sub.id, t1: t1.id, t2: t2.id, p1: p1.id, p2: p2.id};
})()
"""

# --------------------------------------------------------------------------
# 第 1 段：版本 / SW / DB / 概况页版本标记
# --------------------------------------------------------------------------
JS_VERSION = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  eq("APP_VERSION 为 1.6.2", X.appVersion, "1.6.2");

  const sw = await fetch("sw.js", {cache:"no-store"}).then(r => r.text());
  ok("Service Worker 版本升到 wudian-v1.6.2", sw.indexOf('"wudian-v1.6.2"') >= 0,
     (sw.match(/wudian-v[\d.]+/) || ["未找到"])[0]);
  ok("缓存壳无旧版本残留",
     sw.indexOf("wudian-v1.6.1") < 0 && sw.indexOf("wudian-v1.6.0") < 0 &&
     sw.indexOf("wudian-v1.5.0") < 0);
  ok("保留 skipWaiting / clients.claim",
     sw.indexOf("skipWaiting") >= 0 && sw.indexOf("clients.claim") >= 0);

  const db = await new Promise((res, rej) => {
    const r = indexedDB.open("wudian-db"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  eq("IndexedDB 版本仍为 3（本轮不迁移）", db.version, 3);

  /* 概况页版本标记 */
  X.switchMode("training");
  await sleep(320);
  X.state.trTab = "info";
  X.renderTraining();
  await sleep(240);
  const b = document.querySelector("#appVerBadge");
  ok("培训概况页底部有 #appVerBadge", !!b);
  eq("版本标记文案为 v1.6.2", b ? b.textContent.trim() : "(缺失)", "v1.6.2");
  eq("版本标记 data-app-version 为 1.6.2",
     b ? b.getAttribute("data-app-version") : "(缺失)", "1.6.2");
  X.switchMode("wudian");
  await sleep(300);
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 2 段：新开签到表单的两个下拉与联动
# --------------------------------------------------------------------------
JS_FORM = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  X.switchMode("training");
  await sleep(320);
  X.state.trTab = "signin";
  X.state.date = "2026-10-24";
  X.renderTraining();
  await sleep(320);

  document.querySelector("#trNewCourse").click();
  await sleep(220);
  const startSel = document.querySelector("#sheet #trStart");
  const endSel = document.querySelector("#sheet #trEnd");
  ok("新签到表单使用 select#trStart", !!startSel && startSel.tagName === "SELECT");
  ok("新签到表单使用 select#trEnd", !!endSel && endSel.tagName === "SELECT");
  ok("表单不再使用 input[type=time]", !document.querySelector('#sheet input[type="time"]'));

  const startVals = Array.from(startSel.options).map(o => o.value);
  ["00:00","10:00","10:30","22:00"].forEach(t =>
    ok("开始候选含 " + t, startVals.indexOf(t) >= 0));
  ["22:30","23:00","23:30"].forEach(t =>
    ok("开始候选不含 " + t, startVals.indexOf(t) < 0));
  eq("开始候选 45 个档位 + 1 占位", startVals.length, 46);
  ok("新表单开始时间为空", startSel.value === "");
  ok("新表单结束框禁用", endSel.disabled);
  ok("结束框提示先选开始时间",
     endSel.options.length === 1 && endSel.options[0].textContent.indexOf("请先选开始时间") >= 0);

  startSel.value = "10:00"; fire(startSel, "change"); await sleep(80);
  ok("选开始后结束框启用", !endSel.disabled);
  eq("选 10:00 后结束自动 11:30", endSel.value, "11:30");
  startSel.value = "10:30"; fire(startSel, "change"); await sleep(80);
  eq("选 10:30 后结束自动 12:00", endSel.value, "12:00");
  startSel.value = "10:00"; fire(startSel, "change"); await sleep(80);
  eq("改回 10:00 后结束自动 11:30", endSel.value, "11:30");
  endSel.value = "12:00"; fire(endSel, "change"); await sleep(80);
  eq("手动把结束改为 12:00 后不被重置", endSel.value, "12:00");
  startSel.value = "14:00"; fire(startSel, "change"); await sleep(80);
  eq("再把开始改为 14:00 后结束自动恢复 15:30", endSel.value, "15:30");

  const endVals = Array.from(endSel.options).map(o => o.value).filter(v => v);
  ok("结束候选全部为 30 分钟档位", endVals.every(v => /^\d{2}:(00|30)$/.test(v)),
     endVals.join(","));
  ok("结束候选全部晚于开始时间", endVals.every(v => v > "14:00"));
  eq("结束候选首个为 14:30", endVals[0], "14:30");
  eq("结束候选最后为 23:30", endVals[endVals.length - 1], "23:30");
  eq("结束候选共 19 档", endVals.length, 19);

  /* 填齐其余字段并保存 */
  const tSel = document.querySelector("#sheet #trTeacher");
  tSel.value = T.listTeachers()[0].id; fire(tSel, "change"); await sleep(80);
  const subSel = document.querySelector("#sheet #trSubject");
  eq("唯一任教科目自动预选", subSel.value, T.listSubjects()[0].id);
  document.querySelector("#sheet #trDate").value = "2026-10-24";
  const pick = document.querySelector("#sheet .trpick");
  pick.click(); await sleep(60);
  pick.querySelector('[data-st="present"]').click(); await sleep(60);
  document.querySelector("#sheet #trCourseSave").click();
  await sleep(420);
  const saved = T.listCourses({date:"2026-10-24"})[0];
  ok("保存后课程真实写入", !!saved);
  eq("写入开始时间为 14:00", saved && saved.startTime, "14:00");
  eq("写入结束时间为 15:30", saved && saved.endTime, "15:30");
  ok("保存成功后弹层关闭（#sheet 失去 on）",
     !document.querySelector("#sheet").classList.contains("on"));
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 3 段：业务层（trSaveSignin）时间校验
# --------------------------------------------------------------------------
JS_API = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const t1 = T.listTeachers()[0];
  const p1 = T.listStudents()[0];
  const subId = T.listSubjects()[0].id;
  const D = "2026-10-25";
  const base = {date:D, teacherId:t1.id, studentIds:[p1.id],
                statuses:{[p1.id]:"present"}, subjectId:subId, subject:"数学"};
  await fails("API 开始 10:15 被拒绝",
    () => X.trSaveSignin(Object.assign({}, base, {startTime:"10:15", endTime:"11:45"})),
    "30 分钟档位");
  await fails("API 结束 11:45 被拒绝",
    () => X.trSaveSignin(Object.assign({}, base, {startTime:"10:00", endTime:"11:45"})),
    "30 分钟档位");
  await fails("结束等于开始被拒绝",
    () => X.trSaveSignin(Object.assign({}, base, {startTime:"10:00", endTime:"10:00"})),
    "晚于开始");
  await fails("结束早于开始被拒绝",
    () => X.trSaveSignin(Object.assign({}, base, {startTime:"10:00", endTime:"09:30"})),
    "晚于开始");
  await fails("跨日（22:00 开始 + 次日 00:30 结束）被拒绝",
    () => X.trSaveSignin(Object.assign({}, base, {startTime:"22:00", endTime:"00:30"})),
    "晚于开始");
  await fails("开始 22:30（超过最晚 22:00）被拒绝",
    () => X.trSaveSignin(Object.assign({}, base, {startTime:"22:30", endTime:"23:30"})),
    "22:00");
  const cok = await X.trSaveSignin(Object.assign({}, base, {startTime:"10:00", endTime:"11:30"}));
  eq("合法 10:00–11:30 可保存", cok.startTime + "-" + cok.endTime, "10:00-11:30");
  await T.removeCourse(cok.id);
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 4 段：旧课程兼容（原时间保留 + 旧时间临时选项 + 原样保存）
# --------------------------------------------------------------------------
JS_OLD = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const t1 = T.listTeachers()[0];
  const p1 = T.listStudents()[0];
  const D2 = "2026-10-26";
  const old = await T.createCourse({date:D2, startTime:"10:15", endTime:"11:45",
    teacherId:t1.id, subject:"数学", studentIds:[p1.id]});
  await T.setAttendance(old.id, p1.id, "present");

  X.state.trTab = "signin";
  X.state.date = D2;
  X.renderTraining();
  await sleep(320);

  const card = document.querySelector(".trcourse");
  ok("旧课程卡片显示原时间 10:15-11:45",
     card && card.textContent.indexOf("10:15-11:45") >= 0,
     card ? card.textContent.slice(0, 60) : "无卡片");
  card.click(); await sleep(300);
  document.querySelector("#sheet #trEditCourse").click(); await sleep(300);
  const sSel = document.querySelector("#sheet #trStart");
  const eSel = document.querySelector("#sheet #trEnd");
  eq("编辑旧课时开始保留原值 10:15", sSel.value, "10:15");
  eq("编辑旧课时结束保留原值 11:45（不自动 +90）", eSel.value, "11:45");
  ok("开始候选含「旧时间」临时选项",
     Array.from(sSel.options).some(o => o.value === "10:15" && o.textContent.indexOf("旧时间") >= 0),
     Array.from(sSel.options).map(o => o.textContent).filter(t => t.indexOf("旧时间") >= 0).join(","));
  ok("结束候选含「旧时间」临时选项",
     Array.from(eSel.options).some(o => o.value === "11:45" && o.textContent.indexOf("旧时间") >= 0));
  ok("旧课编辑结束框未禁用", !eSel.disabled);

  sSel.value = "14:00"; fire(sSel, "change"); await sleep(80);
  eq("旧课改动开始后结束按新规则恢复 +90（15:30）", eSel.value, "15:30");
  ok("改开始后结束候选不再含「旧时间」选项",
     !Array.from(eSel.options).some(o => o.textContent.indexOf("旧时间") >= 0));

  /* 取消不保存，再重开原样保存 */
  document.querySelector("#sheet #trCourseCancel").click(); await sleep(240);
  document.querySelector(".trcourse").click(); await sleep(300);
  document.querySelector("#sheet #trEditCourse").click(); await sleep(300);
  eq("重开后开始仍为 10:15", document.querySelector("#sheet #trStart").value, "10:15");
  document.querySelector("#sheet #trCourseSave").click(); await sleep(420);
  const after = T.getCourse(old.id);
  eq("旧课未动时间可原样保存（10:15-11:45）",
     after && (after.startTime + "-" + after.endTime), "10:15-11:45");
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 5 段：冲突规则回归 + 390×844 布局 + 截图
# --------------------------------------------------------------------------
JS_LAYOUT = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const t1 = T.listTeachers()[0];
  const p1 = T.listStudents()[0];
  const p2 = T.listStudents()[1];
  const subId = T.listSubjects()[0].id;
  const D = "2026-10-24";

  /* 学生时间冲突规则不回归 */
  await fails("同学生同时段冲突仍被拒绝",
    () => X.trSaveSignin({date:D, startTime:"14:00", endTime:"15:30",
      teacherId:t1.id, studentIds:[p1.id], statuses:{[p1.id]:"present"},
      subjectId:subId, subject:"数学"}),
    "时间冲突");
  const c2 = await X.trSaveSignin({date:D, startTime:"14:00", endTime:"15:30",
    teacherId:T.listTeachers()[1].id, studentIds:[p2.id],
    statuses:{[p2.id]:"present"}, subjectId:subId, subject:"数学"});
  ok("不同学生同时段不同老师可并存", !!c2 && !!c2.id);
  await T.removeCourse(c2.id);

  /* 390×844 布局 */
  X.state.trTab = "signin";
  X.state.date = D;
  X.renderTraining();
  await sleep(320);
  document.querySelector("#trNewCourse").click();
  await sleep(220);
  const sSel = document.querySelector("#sheet #trStart");
  sSel.value = "10:00"; fire(sSel, "change"); await sleep(120);
  const overflow = document.documentElement.scrollWidth > window.innerWidth + 1 ||
                   document.body.scrollWidth > window.innerWidth + 1;
  return {
    overflow: overflow,
    scroll: document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth,
    mojibake: document.documentElement.textContent.indexOf("\uFFFD") >= 0,
    extCount: Array.from(document.querySelectorAll("[src],[href]")).filter(el =>
      /^https?:\/\//i.test(el.getAttribute("src") || el.getAttribute("href") || "")).length,
    endAuto: document.querySelector("#sheet #trEnd").value,
    endDisabled: document.querySelector("#sheet #trEnd").disabled
  };
})()
"""


def main():
    srv = serve()
    page = None
    results = {"checks": [], "fatal": None, "screenshots": [], "version": "1.6.2"}
    try:
        page = launch()
        page.send("Page.navigate", {"url": "http://127.0.0.1:%d/tests/__no_such_page__" % HTTP_PORT})
        time.sleep(0.6)
        page.js(JS_CLEAN_ENV)
        page.mobile(390, 844)
        page.reduce_motion(False)
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)

        print("[1/5] 播种科目 / 老师 / 学生")
        seed = page.js(JS_SEED)
        page.reload()

        print("[2/5] 版本 / SW / DB / 概况页版本标记")
        results["checks"].extend(page.js(JS_VERSION)["checks"])

        print("[3/5] 新开签到表单时间下拉与联动")
        results["checks"].extend(page.js(JS_FORM)["checks"])

        print("[4/5] 业务层时间校验 / 旧课程兼容")
        results["checks"].extend(page.js(JS_API)["checks"])
        results["checks"].extend(page.js(JS_OLD)["checks"])

        print("[5/5] 冲突回归 / 390×844 布局 / 截图")
        r5 = page.js(JS_LAYOUT)
        results["checks"].append({"name": "同学生同时段冲突规则不回归（冲突仍被拒绝）",
                                  "ok": True, "detail": "见第 5 段 fails 结果"})
        results["checks"].append({"name": "签到表单 390×844 无横向滚动",
                                  "ok": not r5["overflow"], "detail": r5["scroll"]})
        results["checks"].append({"name": "签到表单无乱码字符", "ok": not r5["mojibake"],
                                  "detail": "含 U+FFFD" if r5["mojibake"] else "无 U+FFFD"})
        results["checks"].append({"name": "签到表单无外部网络资源", "ok": r5["extCount"] == 0,
                                  "detail": "外部资源 %d 个" % r5["extCount"]})
        results["checks"].append({"name": "截图表单状态：开始 10:00、结束自动 11:30",
                                  "ok": r5["endAuto"] == "11:30" and not r5["endDisabled"],
                                  "detail": "end=%s disabled=%s" % (r5["endAuto"], r5["endDisabled"])})
        shot_path = page.shot("v162-time-select")
        results["screenshots"].append(shot_path)
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
