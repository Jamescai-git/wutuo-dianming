# -*- coding: utf-8 -*-
"""午托点名 v1.6.3 —— 请假不计上课，学生 / 老师 / 概况 / 签到页统计口径统一 端到端自测

真机（无头 Edge + CDP，390×844）验证：
  1. APP_VERSION=1.6.3、SW 缓存版本 wudian-v1.6.3、DB_VER 仍为 3（不迁移）、概况底部 v1.6.3
  2. 同一学生 出勤 1 节 + 迟到 1 节 + 请假 1 节：total=2、scheduled=3、present=1、late=1、leave=1
  3. 学生按老师 / 按科目只含出勤与迟到，不含请假课
  4. 出勤改请假：上课 -1、请假 +1；改回出勤恢复；重复写同一状态不重复计数
  5. 一节课 出勤 + 请假：老师 total=1、people=1、present=1、leave=1
  6. 另一节全体请假：老师 total 不增、people 不增、leave 增加
  7. 老师 byDate / bySubject 只累计实际授课；平均人数分母为实际授课节数
  8. 概况「老师上课次数」与 trTeacherStats().total 一致，全请假课程不计
  9. 签到页顶部显示「上课人次」，值只等于 出勤 + 迟到
 10. CSV 明细仍保留请假行与「请假」状态
 11. 390×844 无横向溢出、无乱码、无外部资源；截图 screenshots/v163-leave-stats.png
 12. 午托点名、培训档案、科目、时间档位、冲突规则、动画回归

用法（本机）：
  C:\\Users\\James\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\e2e_v163_leave_stats.py
依赖：标准库 + websocket-client（无头 Edge 用系统安装的 msedge.exe）
"""
import base64
import glob
import json
import os
import shutil
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
HTTP_PORT = 8116
CDP_PORT = 9462
STAMP = time.strftime("%Y%m%d-%H%M%S")
PROFILE = os.path.join(ROOT, "tests", "_e2e_v163_profile_%s" % STAMP)
DOWNLOAD_DIR = os.path.join(ROOT, "tests", "_e2e_v163_downloads_%s" % STAMP)
SHOT_DIR = os.path.join(ROOT, "screenshots")
REPORT = os.path.join(ROOT, "tests", "last-run-v163-leave.json")


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

    def downloads(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            self.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": path})
        except Exception:
            self.send("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": path})

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
const errText = async (fn) => {
  try { await fn(); return ""; }
  catch (e) { return String((e && e.message) || e); }
};
const viewText = () => document.querySelector("#view").textContent;
/* 读统计卡片里某个标签对应的数字（找 .cell / .box 里 span 文案匹配的那个 b） */
const cellValue = (label) => {
  const cells = Array.from(document.querySelectorAll("#view .cell, #view .box"));
  const hit = cells.find(c => {
    const s = c.querySelector("span");
    return s && s.textContent.replace(/\s/g, "") === label;
  });
  const b = hit && hit.querySelector("b");
  return b ? b.textContent.trim() : "(缺失)";
};
"""

# --------------------------------------------------------------------------
# 第 1 段：版本 / SW / DB / 概况版本标记
# --------------------------------------------------------------------------
JS_VERSION = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  eq("APP_VERSION 为 1.6.3", X.appVersion, "1.6.3");

  const sw = await fetch("sw.js", {cache:"no-store"}).then(r => r.text());
  ok("Service Worker 版本升到 wudian-v1.6.3", sw.indexOf('"wudian-v1.6.3"') >= 0,
     (sw.match(/wudian-v[\d.]+/) || ["未找到"])[0]);
  ok("缓存壳无 v1.6.2 及更早版本残留",
     sw.indexOf("wudian-v1.6.2") < 0 && sw.indexOf("wudian-v1.6.1") < 0 &&
     sw.indexOf("wudian-v1.5.0") < 0);
  ok("保留 skipWaiting / clients.claim 与 network-first 导航",
     sw.indexOf("skipWaiting") >= 0 && sw.indexOf("clients.claim") >= 0 &&
     sw.indexOf('req.mode === "navigate"') >= 0);

  const db = await new Promise((res, rej) => {
    const r = indexedDB.open("wudian-db"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  eq("IndexedDB 版本仍为 3（本轮不迁移）", db.version, 3);

  X.switchMode("training");
  await sleep(320);
  X.state.trTab = "info";
  X.renderTraining();
  await sleep(240);
  const b = document.querySelector("#appVerBadge");
  ok("培训概况页底部有 #appVerBadge", !!b);
  eq("版本标记文案为 v1.6.3", b ? b.textContent.trim() : "(缺失)", "v1.6.3");
  eq("版本标记 data-app-version 为 1.6.3",
     b ? b.getAttribute("data-app-version") : "(缺失)", "1.6.3");
  ok("概况说明写明请假不计上课",
     viewText().indexOf("只有出勤 / 迟到才算实际上课") >= 0);
  X.switchMode("wudian");
  await sleep(260);
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 2 段：播种 + 学生统计口径
# --------------------------------------------------------------------------
JS_SEED_STUDENT = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  await T.restoreSnapshot({teachers:[], students:[], courses:[], attendance:[]});

  const S1 = await T.createSubject({name:"数学", note:"v163 自测"});
  const S2 = await T.createSubject({name:"英语"});
  const T1 = await T.createTeacher({name:"口径老师", subjectIds:[S1.id]});
  const T2 = await T.createTeacher({name:"全勤老师", subjectIds:[S2.id]});
  const P1 = await T.createStudent({name:"张小请", gradeClass:"五年级1班"});
  const P2 = await T.createStudent({name:"李小到", gradeClass:"五年级1班"});
  const P3 = await T.createStudent({name:"王小假", gradeClass:"五年级2班"});

  const D = "2026-10-28";
  /* P1：出勤 + 迟到 + 请假 各一节（同一老师同一科目） */
  const C1 = await T.createCourse({date:D, startTime:"08:00", endTime:"09:30",
    teacherId:T1.id, subjectId:S1.id, subject:"数学", studentIds:[P1.id, P2.id]});
  const C2 = await T.createCourse({date:D, startTime:"10:00", endTime:"11:30",
    teacherId:T1.id, subjectId:S1.id, subject:"数学", studentIds:[P1.id]});
  const C3 = await T.createCourse({date:D, startTime:"13:00", endTime:"14:30",
    teacherId:T1.id, subjectId:S1.id, subject:"数学", studentIds:[P1.id]});
  await T.setAttendance(C1.id, P1.id, "present");
  await T.setAttendance(C1.id, P2.id, "present");
  await T.setAttendance(C2.id, P1.id, "late");
  await T.setAttendance(C3.id, P1.id, "leave");

  const st = X.trStudentStats(P1.id, {from:D, to:D});
  eq("学生上课次数 = 出勤 + 迟到 = 2", st.total, 2);
  eq("学生区间排课数 scheduled = 3", st.scheduled, 3);
  eq("学生出勤 1", st.present, 1);
  eq("学生迟到 1", st.late, 1);
  eq("学生请假 1", st.leave, 1);
  eq("total 严格等于 present + late", st.total, st.present + st.late);

  /* 按老师 / 按科目只含实际到课 */
  eq("按老师只有 1 位（请假课不计）", Object.keys(st.byTeacher).length, 1);
  eq("按老师次数 = 2（不含请假课）", st.byTeacher[T1.id], 2);
  eq("按科目只有 1 组", Object.keys(st.bySubject).length, 1);
  eq("按科目次数 = 2（不含请假课）", Object.values(st.bySubject)[0], 2);
  eq("最近上课只列 2 节实际到课", st.courses.length, 2);
  ok("最近上课不含请假那节",
     st.courses.every(c => c.id !== C3.id), st.courses.map(c => c.id).join(","));

  /* 学生统计界面：上课次数与三种状态同屏显示 */
  X.switchMode("training");
  await sleep(300);
  X.state.trTab = "stats";
  X.state.trStatsMode = "student";
  X.state.trStatsStudentId = P1.id;
  X.state.trStatsPeriod = "month";
  X.state.date = D;
  X.renderTraining();
  await sleep(320);
  eq("界面上课次数显示 2", cellValue("上课次数"), "2");
  eq("界面出勤显示 1", cellValue("出勤"), "1");
  eq("界面迟到显示 1", cellValue("迟到"), "1");
  eq("界面请假显示 1", cellValue("请假"), "1");

  return {checks, ids:{S1:S1.id, S2:S2.id, T1:T1.id, T2:T2.id,
                       P1:P1.id, P2:P2.id, P3:P3.id,
                       C1:C1.id, C2:C2.id, C3:C3.id, D:D}};
})()
"""

# --------------------------------------------------------------------------
# 第 3 段：状态改判幂等（出勤 → 请假 → 出勤）
# --------------------------------------------------------------------------
JS_TOGGLE = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const I = window.__ids;
  const D = I.D;
  const before = X.trStudentStats(I.P1, {from:D, to:D});

  const r1 = await T.setAttendance(I.C1, I.P1, "leave");
  ok("出勤改请假是一次真实写入", r1.changed === true);
  const mid = X.trStudentStats(I.P1, {from:D, to:D});
  eq("改判后上课次数 -1", mid.total, before.total - 1);
  eq("改判后请假次数 +1", mid.leave, before.leave + 1);
  eq("改判后出勤 -1", mid.present, before.present - 1);
  eq("改判不改变排课数", mid.scheduled, before.scheduled);

  const r2 = await T.setAttendance(I.C1, I.P1, "leave");
  ok("重复写同一状态被幂等拦下（changed=false）", r2.changed === false);
  const dup = X.trStudentStats(I.P1, {from:D, to:D});
  eq("重复写不重复扣上课次数", dup.total, mid.total);
  eq("重复写不重复加请假次数", dup.leave, mid.leave);

  const r3 = await T.setAttendance(I.C1, I.P1, "present");
  ok("改回出勤是真实写入", r3.changed === true);
  const back = X.trStudentStats(I.P1, {from:D, to:D});
  eq("改回出勤后上课次数恢复", back.total, before.total);
  eq("改回出勤后请假次数恢复", back.leave, before.leave);
  eq("改回出勤后出勤次数恢复", back.present, before.present);

  /* 迟到改请假同样只减一次 */
  await T.setAttendance(I.C2, I.P1, "leave");
  const late2 = X.trStudentStats(I.P1, {from:D, to:D});
  eq("迟到改请假：上课次数只剩 1", late2.total, 1);
  eq("迟到改请假：请假累计 2", late2.leave, 2);
  await T.setAttendance(I.C2, I.P1, "late");
  eq("迟到改回后上课次数复原", X.trStudentStats(I.P1, {from:D, to:D}).total, 2);
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 4 段：老师统计（含出勤 + 请假混合、全请假课）
# --------------------------------------------------------------------------
JS_TEACHER = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const I = window.__ids;
  const D = I.D;
  const S2 = I.S2;

  /* 第 5 项：一节 出勤 + 请假 */
  const C4 = await T.createCourse({date:D, startTime:"15:00", endTime:"16:30",
    teacherId:I.T2, subjectId:S2, subject:"英语", studentIds:[I.P2, I.P3]});
  await T.setAttendance(C4.id, I.P2, "present");
  await T.setAttendance(C4.id, I.P3, "leave");

  const t1 = X.trTeacherStats(I.T2, {from:D, to:D});
  eq("混合课：老师上课节数 1", t1.total, 1);
  eq("混合课：授课人次只算出勤 = 1", t1.people, 1);
  eq("混合课：出勤人次 1", t1.present, 1);
  eq("混合课：请假人次 1", t1.leave, 1);

  /* 第 6 项：另一节全体请假 */
  const C5 = await T.createCourse({date:D, startTime:"17:00", endTime:"18:30",
    teacherId:I.T2, subjectId:S2, subject:"英语", studentIds:[I.P2, I.P3]});
  await T.setAttendance(C5.id, I.P2, "leave");
  await T.setAttendance(C5.id, I.P3, "leave");
  const t2 = X.trTeacherStats(I.T2, {from:D, to:D});
  eq("全请假课不计老师上课节数", t2.total, 1);
  eq("全请假课不增授课人次", t2.people, 1);
  eq("全请假课请假人次累计到 3", t2.leave, 3);
  eq("排课数仍记录 2 节（内部核对）", t2.scheduled, 2);

  /* 第 7 项：byDate / bySubject 只累计实际授课；平均人数分母为实际授课节数 */
  eq("按日期只计 1 节实际授课", t2.byDate[D], 1);
  eq("按科目只有 1 组", Object.keys(t2.bySubject).length, 1);
  eq("按科目只计 1 节实际授课", Object.values(t2.bySubject)[0], 1);
  eq("平均人数分母用实际授课节数（1/1=1.0）", (t2.people / t2.total).toFixed(1), "1.0");
  eq("最近课程只列实际授课的 1 节", t2.courses.length, 1);
  ok("最近课程不含全请假那节", t2.courses.every(c => c.id !== C5));

  /* 老师统计界面：平均人数显示 1.0，请假仍是 3 */
  X.state.trTab = "stats";
  X.state.trStatsMode = "teacher";
  X.state.trStatsTeacherId = I.T2;
  X.state.trStatsPeriod = "month";
  X.renderTraining();
  await sleep(320);
  eq("界面老师上课节数 1", cellValue("上课节数"), "1");
  eq("界面授课人次 1", cellValue("授课人次"), "1");
  eq("界面平均人数 1.0", cellValue("平均人数"), "1.0");
  eq("界面请假人次 3", cellValue("请假"), "3");
  ok("按日期区块只列 1 节", /2026-10-28[\s\S]{0,20}1 节/.test(viewText()), viewText().slice(0, 200));

  /* 第 8 项：概况「老师上课次数」与 trTeacherStats().total 完全一致 */
  const counts = X.trTeacherCourseCounts();
  const allTeachers = [I.T1, I.T2];
  allTeachers.forEach(tid => {
    const ts = X.trTeacherStats(tid, {from:"2026-10-01", to:"2026-10-31"});
    eq("概况口径 = 老师统计口径（" + T.getTeacher(tid).name + "）",
       (counts[tid] || 0), ts.total);
  });
  eq("口径老师实际授课 2 节（含 1 节请假不算）", counts[I.T1], 2);
  eq("全勤老师实际授课 1 节（全请假课不算）", counts[I.T2], 1);

  X.state.trTab = "info";
  X.renderTraining();
  await sleep(280);
  const rows = Array.from(document.querySelectorAll("#view .drow"));
  const rowOf = (name) => rows.find(r => r.textContent.indexOf(name) >= 0);
  ok("概况列出全勤老师", !!rowOf("全勤老师"));
  ok("概况全勤老师显示 1 节（全请假课不计）",
     !!rowOf("全勤老师") && rowOf("全勤老师").textContent.indexOf("1 节") >= 0,
     rowOf("全勤老师") ? rowOf("全勤老师").textContent : "(缺失)");
  ok("概况口径老师显示 2 节",
     !!rowOf("口径老师") && rowOf("口径老师").textContent.indexOf("2 节") >= 0,
     rowOf("口径老师") ? rowOf("口径老师").textContent : "(缺失)");

  window.__c4 = C4.id; window.__c5 = C5.id;
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 5 段：签到页汇总「上课人次」+ 390×844 布局 + 截图
# --------------------------------------------------------------------------
JS_SIGNIN = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const I = window.__ids;
  const D = I.D;
  X.state.trTab = "signin";
  X.state.date = D;
  X.renderTraining();
  await sleep(340);

  const boxes = Array.from(document.querySelectorAll("#view .trsum .box"))
    .map(b => ({v:(b.querySelector("b") || {}).textContent, k:(b.querySelector("span") || {}).textContent}));
  const label = boxes.map(b => b.k).join("|");
  ok("顶部汇总显示「上课人次」", label.indexOf("上课人次") >= 0, label);
  ok("顶部不再显示「签到人次」", label.indexOf("签到人次") < 0, label);

  const byKey = {};
  boxes.forEach(b => { byKey[b.k] = Number(b.v); });
  /* 当日真实：课程 5 节（含 1 节全请假）；出勤 3、迟到 1、请假 4、未归 0 */
  eq("当日课程仍为 5（全请假课照常列出）", byKey["当日课程"], 5);
  eq("上课人次 = 出勤 3 + 迟到 1 = 4", byKey["上课人次"], 4);
  eq("出勤显示真实数量 3", byKey["出勤"], 3);
  eq("迟到显示真实数量 1", byKey["迟到"], 1);
  eq("请假显示真实数量 4", byKey["请假"], 4);
  eq("未归状态 0", byKey["未归状态"], 0);
  eq("上课人次严格等于出勤 + 迟到",
     byKey["上课人次"], byKey["出勤"] + byKey["迟到"]);
  ok("上课人次不等于学生总数（请假被排除）", byKey["上课人次"] !== 8,
     String(byKey["上课人次"]));
  eq("课程卡片仍是 5 张", document.querySelectorAll(".trcourse").length, 5);
  ok("全请假课程卡片仍可见（未隐藏）",
     viewText().indexOf("17:00") >= 0 && viewText().indexOf("请假 2") >= 0,
     viewText().replace(/\s+/g, " ").slice(0, 260));

  const overflow = document.documentElement.scrollWidth > window.innerWidth + 1 ||
                   document.body.scrollWidth > window.innerWidth + 1;
  return {
    overflow: overflow,
    scroll: document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth,
    mojibake: document.documentElement.textContent.indexOf("\uFFFD") >= 0,
    extCount: Array.from(document.querySelectorAll("[src],[href]")).filter(el =>
      /^https?:\/\//i.test(el.getAttribute("src") || el.getAttribute("href") || "")).length,
    checks: checks
  };
})()
"""

# --------------------------------------------------------------------------
# 第 6 段：CSV 明细仍含请假行
# --------------------------------------------------------------------------
JS_CSV = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  X.state.trTab = "signin";
  X.state.date = window.__ids.D;
  X.renderTraining();
  await sleep(300);
  document.querySelector("#trExportToday").click();
  await sleep(1200);
  return {checks:[{name:"已触发当日签到 CSV 导出", ok:true, detail:"等待下载落盘"}]};
})()
"""

# --------------------------------------------------------------------------
# 第 7 段：回归（午托 / 档案 / 科目 / 时间档位 / 冲突 / 动画）
# --------------------------------------------------------------------------
JS_REGRESS = r"""
(async () => {
""" + JS_HELPERS + r"""
  await T.ready();
  const I = window.__ids;

  /* 冲突规则 */
  const conflict = await errText(() => X.trSaveSignin({
    date:I.D, startTime:"08:30", endTime:"09:00", teacherId:I.T2,
    studentIds:[I.P1], statuses:{[I.P1]:"present"}, subjectId:I.S2, subject:"英语"
  }));
  ok("学生时间冲突仍被拒绝", conflict.indexOf("时间冲突") >= 0, conflict);
  const noSub = await errText(() => X.trSaveSignin({
    date:"2026-10-29", startTime:"08:00", endTime:"09:30", teacherId:I.T2,
    studentIds:[I.P3], statuses:{[I.P3]:"present"}
  }));
  ok("未选科目仍被拒绝", noSub.indexOf("请选择科目") >= 0, noSub);

  /* 时间档位：仍是 30 分钟下拉 + 默认 90 分钟 */
  X.state.trTab = "signin";
  X.state.date = "2026-10-29";
  X.renderTraining();
  await sleep(300);
  document.querySelector("#trNewCourse").click();
  await sleep(240);
  const sSel = document.querySelector("#sheet #trStart");
  const eSel = document.querySelector("#sheet #trEnd");
  ok("开始时间仍是下拉", !!sSel && sSel.tagName === "SELECT");
  ok("结束时间仍是下拉", !!eSel && eSel.tagName === "SELECT");
  ok("开始候选 45 档 + 占位", sSel.options.length === 46, sSel.options.length);
  ok("结束框初始禁用", eSel.disabled);
  sSel.value = "10:00"; fire(sSel, "change"); await sleep(100);
  eq("选 10:00 后结束自动 11:30（默认 90 分钟不回归）", eSel.value, "11:30");
  document.querySelector("#sheet [data-close]").click();
  await sleep(200);

  /* 档案页三页签 + 科目行 */
  X.state.trTab = "archives";
  X.state.trArchSeg = "students";
  X.state.trSearch = "";
  X.renderTraining();
  await sleep(300);
  ok("档案内部三页签存在", !!document.querySelector("#trArchSeg") &&
     document.querySelectorAll("#trArchSeg button").length === 3);
  eq("学生档案 3 行", document.querySelectorAll("#view .row").length, 3);
  document.querySelector('#trArchSeg button[data-seg="subjects"]').click();
  await sleep(280);
  ok("科目档案仍渲染数学 / 英语",
     viewText().indexOf("数学") >= 0 && viewText().indexOf("英语") >= 0);
  document.querySelector('#trArchSeg button[data-seg="teachers"]').click();
  await sleep(260);
  eq("老师档案 2 行", document.querySelectorAll("#view .row").length, 2);

  /* 午托点名模式不受影响：真机加一名午托学生，再点名一次 */
  X.switchMode("wudian");
  await sleep(360);
  eq("午托底部三项导航", document.querySelectorAll("#tabBar .tab").length, 3);
  ok("午托状态笔仍在", !!document.querySelector("#brushBar"));
  document.querySelector('.tab[data-tab="roster"]').click();
  await sleep(300);
  document.querySelector("#addStu").click();
  await sleep(320);
  document.querySelector("#fName").value = "午托检查";
  document.querySelector("#fGroup").value = "三年级1班";
  document.querySelector("#saveStu").click();
  await sleep(700);
  eq("午托新增学生成功", X.state.students.length, 1);
  document.querySelector('.tab[data-tab="roll"]').click();
  await sleep(340);
  eq("午托点名渲染 1 张卡片", document.querySelectorAll("#grid .card").length, 1);
  const wCard = document.querySelector("#grid .card");
  wCard.click();
  await sleep(460);
  eq("午托点名点击写入 1 条记录", X.state.records.size, 1);
  ok("午托点名卡片进入已点状态",
     String(wCard.getAttribute("data-st") || "") !== "" ||
     String(wCard.className).indexOf("st-") >= 0,
     wCard.getAttribute("data-st") + " / " + wCard.className);
  const wOv = document.documentElement.scrollWidth > window.innerWidth + 1;
  ok("午托点名页 390×844 无横向溢出", !wOv,
     document.documentElement.scrollWidth + "/" + window.innerWidth);

  /* 动画口径未被改动 */
  ok("重排动画时长仍 320ms", X.motion.duration === 320, X.motion.duration);
  ok("补位动画 70ms 延迟 / 260ms 时长",
     X.motion.floatDelay === 70 && X.motion.floatDuration === 260,
     X.motion.floatDelay + "/" + X.motion.floatDuration);
  ok("减少动态效果分支仍在", typeof X.motion.reduceDuration === "number" &&
     X.motion.reduceDuration > 0, X.motion.reduceDuration);

  /* 统计口径统一自检：学生 total 与 老师 people 都只含出勤 + 迟到 */
  const stu = X.trStudentStats(I.P1, {from:I.D, to:I.D});
  const tea = X.trTeacherStats(I.T1, {from:I.D, to:I.D});
  eq("学生 total 只含出勤 + 迟到", stu.total, stu.present + stu.late);
  eq("老师 people 只含出勤 + 迟到（口径老师 3）", tea.people, tea.present + tea.late);
  eq("口径老师授课人次 = 3", tea.people, 3);
  return {checks};
})()
"""


def read_csv(directory):
    files = sorted(glob.glob(os.path.join(directory, "*.csv")), key=os.path.getmtime)
    if not files:
        return None, None
    path = files[-1]
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8-sig")
    return path, text


def main():
    srv = serve()
    page = None
    results = {"checks": [], "fatal": None, "screenshots": [], "version": "1.6.3"}
    try:
        page = launch()
        page.send("Page.navigate", {"url": "http://127.0.0.1:%d/tests/__no_such_page__" % HTTP_PORT})
        time.sleep(0.6)
        page.js(JS_CLEAN_ENV)
        page.mobile(390, 844)
        page.reduce_motion(False)
        page.downloads(DOWNLOAD_DIR)
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)

        print("[1/7] 版本 / SW / DB / 概况版本标记")
        results["checks"].extend(page.js(JS_VERSION)["checks"])

        print("[2/7] 播种 + 学生统计口径（出勤 / 迟到 / 请假）")
        seed = page.js(JS_SEED_STUDENT)
        results["checks"].extend(seed["checks"])
        page.js("window.__ids = %s;" % json.dumps(seed["ids"]))
        page.reload()
        page.js("window.__ids = %s;" % json.dumps(seed["ids"]))

        print("[3/7] 状态改判幂等")
        results["checks"].extend(page.js(JS_TOGGLE)["checks"])

        print("[4/7] 老师统计 / 概况口径")
        results["checks"].extend(page.js(JS_TEACHER)["checks"])

        print("[5/7] 签到页「上课人次」汇总")
        r5 = page.js(JS_SIGNIN)
        results["checks"].extend(r5["checks"])
        results["checks"].append({"name": "签到页 390×844 无横向滚动",
                                  "ok": not r5["overflow"], "detail": r5["scroll"]})
        results["checks"].append({"name": "签到页无乱码字符", "ok": not r5["mojibake"],
                                  "detail": "含 U+FFFD" if r5["mojibake"] else "无 U+FFFD"})
        results["checks"].append({"name": "签到页无外部网络资源", "ok": r5["extCount"] == 0,
                                  "detail": "外部资源 %d 个" % r5["extCount"]})

        print("[6/7] CSV 明细保留请假行")
        results["checks"].extend(page.js(JS_CSV)["checks"])
        csv_path, csv_text = read_csv(DOWNLOAD_DIR)
        results["checks"].append({"name": "CSV 文件已落盘",
                                  "ok": bool(csv_path), "detail": csv_path or "(未找到)"})
        if csv_text is not None:
            lines = [x for x in csv_text.splitlines() if x.strip()]
            results["checks"].append({"name": "CSV 行数 = 表头 + 8 条明细（不删减）",
                                      "ok": len(lines) == 9, "detail": "%d 行" % len(lines)})
            results["checks"].append({"name": "CSV 仍保留请假状态",
                                      "ok": "请假" in csv_text,
                                      "detail": "含请假 %d 次" % csv_text.count("请假")})
            results["checks"].append({"name": "CSV 请假行指向对应学生与课程",
                                      "ok": "13:00,14:30" in csv_text and "张小请" in csv_text,
                                      "detail": "含 13:00,14:30 与 张小请"})
            results["artifactsCsv"] = csv_path

        print("[7/7] 截图 / 回归（午托 / 档案 / 科目 / 时间档位 / 冲突 / 动画）")
        # 截图前回到学生统计页，直观展示 上课次数 2 / 出勤 1 / 迟到 1 / 请假 1
        page.js("""(async () => {
          const X = window.__test;
          X.state.mode === "training" || X.switchMode("training");
          await new Promise(r => setTimeout(r, 320));
          X.state.trTab = "stats"; X.state.trStatsMode = "student";
          X.state.trStatsStudentId = window.__ids.P1; X.state.trStatsPeriod = "month";
          X.state.date = window.__ids.D; X.renderTraining();
          await new Promise(r => setTimeout(r, 340));
          return true;
        })()""")
        shot_path = page.shot("v163-leave-stats")
        results["screenshots"].append(shot_path)

        results["checks"].extend(page.js(JS_REGRESS)["checks"])
    except Exception as e:  # noqa: BLE001
        results["fatal"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if page:
            page.close()
        srv.shutdown()
        shutil.rmtree(PROFILE, ignore_errors=True)
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

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
