# -*- coding: utf-8 -*-
"""午托点名 v1.5 —— 任务 B：两套模式隔离与两套底部导航 端到端自测

真机（无头 Edge + CDP）验证（390×844）：
  1. 午托模式底部恰好三项：点名 / 名单 / 记录；DOM 中无任何培训底部按钮
  2. 培训模式底部恰好四项：签到 / 档案 / 统计 / 概况；DOM 中无任何午托底部按钮
  3. 统计、概况从培训底部直接进入；档案内 学生/老师/科目 三个内部页签
  4. 午托状态笔、进度条、午托计数条只在午托模式（且点名页）出现
  5. 模式来回切换各自记住上次页面；刷新后仍然记住
  6. 390×844 两套导航均无横向滚动、无乱码、无外部资源
  7. 截图：培训签到页、培训统计页、午托点名页

用法（本机）：
  C:\\Users\\James\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\e2e_v15_navigation.py
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
HTTP_PORT = 8107
CDP_PORT = 9452
PROFILE = os.path.join(ROOT, "tests", "_e2e_v15_profile")
SHOT_DIR = os.path.join(ROOT, "tests", "screenshots")
REPORT = os.path.join(ROOT, "tests", "last-run-v15-nav.json")


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
            time.sleep(0.3)
        raise RuntimeError("页面未在 %ss 内就绪" % secs)

    def goto(self, url):
        self.send("Page.navigate", {"url": url})
        time.sleep(0.8)
        self.wait_ready()

    def mobile(self, w=390, h=844):
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1,
            "mobile": True, "screenWidth": w, "screenHeight": h
        })

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


def launch(profile=PROFILE, fresh=True):
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
# 第 1 段：两套模式的按钮互斥 / 底部直达 / 档案内页签 / 记忆
# --------------------------------------------------------------------------
JS_NAV = r"""
(async () => {
  const X = window.__test, T = window.Training;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const hasTab = t => !!document.querySelector(`#tabBar .tab[data-tab="${t}"]`);
  const tabs = () => Array.from(document.querySelectorAll("#tabBar .tab")).map(b => b.dataset.tab).join(",");

  await T.ready();
  /* 从午托模式开始（profile 残留偏好也归位，页签记忆重置为默认） */
  X.switchMode("wudian");
  X.state.trTab = "signin";
  X.state.tab = "roll";
  await sleep(350);

  /* ---------- 1. 午托模式 ---------- */
  eq("归位为午托模式", X.state.mode, "wudian");
  eq("午托底部恰好三项", document.querySelectorAll("#tabBar .tab").length, 3);
  eq("午托底部为 点名/名单/记录", tabs(), "roll,roster,records");
  ok("午托模式 DOM 无培训底部按钮", ["signin","archives","stats","info"].every(t => !hasTab(t)));
  ok("旧第四标签「培训」已移除", !document.querySelector('.tab[data-tab="training"]'));
  ok("顶部模式切换二段存在", !!document.querySelector('#modeSeg button[data-mode="wudian"]') &&
     !!document.querySelector('#modeSeg button[data-mode="training"]'));
  ok("午托模式高亮午托", document.querySelector('#modeSeg button[data-mode="wudian"]').classList.contains("on"));

  /* 点名页 chrome：状态笔 / 进度 / 计数都在 */
  document.querySelector('.tab[data-tab="roll"]').click(); await sleep(300);
  ok("点名页显示午托状态笔", getComputedStyle(document.querySelector("#brushBar")).display !== "none");
  ok("点名页显示点名进度", getComputedStyle(document.querySelector("#progBar")).display !== "none");
  ok("点名页显示午托计数条", getComputedStyle(document.querySelector("#statsBar")).display !== "none");

  /* 记录页：状态笔 / 进度收起，计数条保留（午托模式内） */
  document.querySelector('.tab[data-tab="records"]').click(); await sleep(300);
  ok("记录页隐藏午托状态笔", getComputedStyle(document.querySelector("#brushBar")).display === "none");
  ok("记录页隐藏点名进度", getComputedStyle(document.querySelector("#progBar")).display === "none");
  ok("记录页仍显示午托计数条", getComputedStyle(document.querySelector("#statsBar")).display !== "none");

  /* ---------- 2. 切培训 ---------- */
  document.querySelector('#modeSeg button[data-mode="training"]').click(); await sleep(350);
  eq("切换到培训模式", X.state.mode, "training");
  ok("培训模式高亮培训", document.querySelector('#modeSeg button[data-mode="training"]').classList.contains("on"));
  eq("培训底部恰好四项", document.querySelectorAll("#tabBar .tab").length, 4);
  eq("培训底部为 签到/档案/统计/概况", tabs(), "signin,archives,stats,info");
  ok("培训模式 DOM 无午托底部按钮", ["roll","roster","records"].every(t => !hasTab(t)));
  eq("培训默认落在签到页", X.state.trTab, "signin");
  ok("培训页隐藏午托状态笔", getComputedStyle(document.querySelector("#brushBar")).display === "none");
  ok("培训页隐藏点名进度", getComputedStyle(document.querySelector("#progBar")).display === "none");
  ok("培训页隐藏且清空午托计数条", getComputedStyle(document.querySelector("#statsBar")).display === "none" &&
     document.querySelector("#statsBar").children.length === 0);
  ok("旧顶部五段导航已移除", !document.querySelector("#trSeg"));
  ok("培训签到页无午托点名卡片网格", !document.querySelector("#grid"));
  ok("培训签到页无状态笔按钮", !document.querySelector("#brushBar .on") &&
     document.querySelector("#brushBar").textContent.trim() === "");

  /* ---------- 3. 统计 / 概况底部直达 ---------- */
  document.querySelector('.tab[data-tab="stats"]').click(); await sleep(320);
  eq("底部直达统计页", X.state.trTab, "stats");
  ok("统计页内容渲染", !!document.querySelector("#trStatsMode") ||
     document.querySelector("#view").textContent.indexOf("上课") >= 0);
  document.querySelector('.tab[data-tab="info"]').click(); await sleep(320);
  eq("底部直达概况页", X.state.trTab, "info");
  ok("概况页内容渲染", document.querySelector("#view").textContent.length > 10);

  /* ---------- 4. 档案内 学生/老师/科目 ---------- */
  document.querySelector('.tab[data-tab="archives"]').click(); await sleep(320);
  eq("档案内部分段恰好三项", document.querySelectorAll("#trArchSeg button").length, 3);
  eq("档案页签为 学生/老师/科目",
     Array.from(document.querySelectorAll("#trArchSeg button")).map(b => b.dataset.seg).join(","),
     "students,teachers,subjects");
  eq("档案默认学生页", X.state.trArchSeg, "students");
  document.querySelector('#trArchSeg button[data-seg="subjects"]').click(); await sleep(320);
  ok("科目页容纳结构渲染", document.querySelector("#view").textContent.indexOf("科目档案") >= 0);
  document.querySelector('#trArchSeg button[data-seg="teachers"]').click(); await sleep(320);
  ok("老师页渲染", document.querySelector("#view").textContent.indexOf("老师") >= 0);
  document.querySelector('#trArchSeg button[data-seg="students"]').click(); await sleep(320);

  /* ---------- 5. 各自记住上次页面 ---------- */
  document.querySelector('.tab[data-tab="stats"]').click(); await sleep(250);   /* 培训停在 stats */
  document.querySelector('#modeSeg button[data-mode="wudian"]').click(); await sleep(350);
  eq("切回午托停在记录页（上次页面）", X.state.tab, "records");
  ok("午托底部按钮回来了", hasTab("roll") && hasTab("roster") && hasTab("records"));
  document.querySelector('.tab[data-tab="roster"]').click(); await sleep(250);  /* 午托改停 roster */
  document.querySelector('#modeSeg button[data-mode="training"]').click(); await sleep(350);
  eq("切回培训停在统计页（上次页面）", X.state.trTab, "stats");
  eq("底部高亮为统计", document.querySelector("#tabBar .tab.on").dataset.tab, "stats");

  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 2 段：刷新后模式与页签记忆持久化
# --------------------------------------------------------------------------
JS_PERSIST = r"""
(async () => {
  const X = window.__test, T = window.Training;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  await T.ready();
  eq("刷新后仍为培训模式", X.state.mode, "training");
  eq("刷新后培训停在统计页", X.state.trTab, "stats");
  eq("刷新后底部高亮为统计", document.querySelector("#tabBar .tab.on").dataset.tab, "stats");
  ok("刷新后培训底部无午托按钮", !document.querySelector('.tab[data-tab="roll"]'));
  document.querySelector('#modeSeg button[data-mode="wudian"]').click();
  await sleep(350);
  eq("切回午托仍记住名单页", X.state.tab, "roster");
  eq("午托底部高亮为名单", document.querySelector("#tabBar .tab.on").dataset.tab, "roster");
  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 3 段：390×844 布局 / 乱码 / 外部资源检查（截图由 Python 侧穿插拍摄）
# --------------------------------------------------------------------------
JS_LAYOUT_TRAINING = r"""
(async () => {
  const X = window.__test;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  X.switchMode("training"); await sleep(350);
  X.switchTab("signin"); await sleep(400);
  return {
    signinOverflow: document.documentElement.scrollWidth > window.innerWidth + 1 ||
                    document.body.scrollWidth > window.innerWidth + 1,
    signinScroll: document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth
  };
})()
"""

JS_LAYOUT_TRAINING_STATS = r"""
(async () => {
  const X = window.__test;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  X.switchTab("stats"); await sleep(400);
  return {
    statsOverflow: document.documentElement.scrollWidth > window.innerWidth + 1 ||
                   document.body.scrollWidth > window.innerWidth + 1,
    statsScroll: document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth
  };
})()
"""

JS_LAYOUT_WUDIAN = r"""
(async () => {
  const X = window.__test;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  X.switchMode("wudian"); await sleep(350);
  X.switchTab("roll"); await sleep(350);
  const pageText = document.documentElement.textContent;
  const ext = Array.from(document.querySelectorAll("[src],[href]")).filter(el => {
    const v = el.getAttribute("src") || el.getAttribute("href") || "";
    return /^https?:\/\//i.test(v);
  });
  return {
    rollOverflow: document.documentElement.scrollWidth > window.innerWidth + 1 ||
                  document.body.scrollWidth > window.innerWidth + 1,
    rollScroll: document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth,
    mojibake: pageText.indexOf("\uFFFD") >= 0,
    extCount: ext.length
  };
})()
"""


def main():
    srv = serve()
    page = None
    results = {"checks": [], "fatal": None, "screenshots": []}
    try:
        page = launch()
        page.send("Page.navigate",
                  {"url": "http://127.0.0.1:%d/tests/__no_such_page__" % HTTP_PORT})
        time.sleep(0.6)
        page.js(JS_CLEAN_ENV)
        page.mobile(390, 844)
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)

        print("[1/4] 模式互斥 / 底部直达 / 档案内页签 / 记忆")
        r1 = page.js(JS_NAV)
        results["checks"].extend(r1["checks"])

        print("[2/4] 刷新验证模式与页签记忆持久化")
        page.send("Page.reload")
        time.sleep(1.0)
        page.wait_ready()
        r2 = page.js(JS_PERSIST)
        results["checks"].extend(r2["checks"])

        print("[3/4] 390×844 布局检查 + 截图（培训签到 / 培训统计 / 午托点名）")
        r3 = page.js(JS_LAYOUT_TRAINING)
        results["checks"].append({"name": "培训签到页 390×844 无横向滚动",
                                  "ok": not r3["signinOverflow"], "detail": r3["signinScroll"]})
        results["screenshots"].append(page.shot("v15-nav-training-signin"))
        r4 = page.js(JS_LAYOUT_TRAINING_STATS)
        results["checks"].append({"name": "培训统计页 390×844 无横向滚动",
                                  "ok": not r4["statsOverflow"], "detail": r4["statsScroll"]})
        results["screenshots"].append(page.shot("v15-nav-training-stats"))
        r5 = page.js(JS_LAYOUT_WUDIAN)
        results["checks"].append({"name": "午托点名页 390×844 无横向滚动",
                                  "ok": not r5["rollOverflow"], "detail": r5["rollScroll"]})
        results["checks"].append({"name": "页面无乱码字符", "ok": not r5["mojibake"],
                                  "detail": "含 U+FFFD" if r5["mojibake"] else "无 U+FFFD"})
        results["checks"].append({"name": "页面无外部网络资源", "ok": r5["extCount"] == 0,
                                  "detail": "外部资源 %d 个" % r5["extCount"]})
        results["screenshots"].append(page.shot("v15-nav-wudian-roll"))

        print("[4/4] 收尾")
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

    print("-" * 66)
    for c in results["checks"]:
        print(("PASS  " if c["ok"] else "FAIL  ") + c["name"] + ("" if c["ok"] else "  <- " + c["detail"]))
    print("-" * 66)
    print("合计 %d 项：通过 %d，失败 %d" % (len(results["checks"]), passed, len(failed)))
    if results["fatal"]:
        print("致命错误：" + results["fatal"])
    print("报告：" + REPORT)
    print("截图：" + ", ".join(results["screenshots"]))
    ok = (not results["fatal"]) and not failed
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
