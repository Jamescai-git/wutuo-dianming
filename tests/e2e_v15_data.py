# -*- coding: utf-8 -*-
"""午托点名 v1.5 —— 科目数据层 / 旧数据迁移 / 备份兼容 独立自测

真机（无头 Edge + CDP）验证：
  0. 真实 IndexedDB v2→v3 升级：同一 origin 先手工建 v2 库并写入午托学生/记录
     与培训老师/课程/签到样例，关闭连接后加载当前 index.html 升到 v3；
     断言 trSubjects 新增、原有八张表与样例数据一条不少、未重建
  1. IndexedDB 升到 v3：trSubjects 新建，原有七张表一个不少
  2. 科目 CRUD：必填 / 归一化去重（空白 + 大小写）/ 编辑 / 启停 / 搜索 / 稳定排序
  3. 删除保护：被老师或课程引用的科目不得硬删除
  4. 老师多科目：subjectIds 读写、返回数组副本、旧 subject 文本兼容
  5. 课程：subjectId 校验 + subjectName 快照；只给旧文本也能用
  6. v1.4 旧快照（无 subjects）恢复：老师 + 课程旧文本都自动建档、补齐关联，
     且幂等；无科目文本 + 悬空 subjectId 的课程清掉悬空 ID、显示兜底不出现
     undefined；科目改名后 reload，历史课程仍保留旧名称快照
  7. v1.5 快照：schema 2、subjects 段完整、恢复后五类数据数量一致

用法（本机）：
  C:\\Users\\James\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\e2e_v15_data.py
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
HTTP_PORT = 8098
CDP_PORT = 9445
PROFILE = os.path.join(ROOT, "tests", "_e2e_v15_profile")
SHOT_DIR = os.path.join(ROOT, "tests", "screenshots")
REPORT = os.path.join(ROOT, "tests", "last-run-v15.json")


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


def _close_stale_cdp():
    """上一轮若有无头 Edge 残留并占着 CDP 端口，先让它退出，避免新实例
    绑定端口失败后静默复用旧实例（旧实例会跑旧版页面代码）。"""
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


# 每个阶段加载页面前先执行的清理：注销 Service Worker + 清 CacheStorage + 清 HTTP 缓存，
# 防止 profile 残留的旧版外壳缓存让页面跑旧代码
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
    # 不在测试进程里删除 profile（本机批量删除保护会按 turn 累计拦截并硬中断）。
    # 陈旧 profile 的 SW/缓存污染由 launch 后的同源 JS_CLEAN_ENV + clearBrowserCache 兜底。
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--remote-debugging-port=%d" % CDP_PORT,
         "--remote-allow-origins=*", "--user-data-dir=" + profile,
         "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--window-size=430,900", "about:blank"],
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


# --------------------------------------------------------------------------
# 第 0 段：真实 IndexedDB v2→v3 升级（独立 profile，先建 v2 再加载现版页面）
# --------------------------------------------------------------------------
JS_MAKE_V2 = r"""
(async () => {
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  /* 按 v1.4 的口径手工建 v2 库：八张表，绝无 trSubjects */
  await new Promise((res, rej) => {
    const req = indexedDB.open("wudian-db", 2);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains("students")) db.createObjectStore("students", {keyPath:"id"});
      if (!db.objectStoreNames.contains("records"))  db.createObjectStore("records",  {keyPath:"key"});
      if (!db.objectStoreNames.contains("avatars"))  db.createObjectStore("avatars",  {keyPath:"id"});
      if (!db.objectStoreNames.contains("kv"))       db.createObjectStore("kv");
      if (!db.objectStoreNames.contains("trTeachers")) db.createObjectStore("trTeachers", {keyPath:"id"});
      if (!db.objectStoreNames.contains("trStudents")) db.createObjectStore("trStudents", {keyPath:"id"});
      if (!db.objectStoreNames.contains("trCourses")) {
        const cs = db.createObjectStore("trCourses", {keyPath:"id"});
        cs.createIndex("byDate", "date", {unique:false});
        cs.createIndex("byTeacher", "teacherId", {unique:false});
      }
      if (!db.objectStoreNames.contains("trAttendance")) {
        const as = db.createObjectStore("trAttendance", {keyPath:"key"});
        as.createIndex("byCourse", "courseId", {unique:false});
      }
    };
    req.onsuccess = () => {
      const db = req.result;
      const tx = db.transaction(
        ["students","records","avatars","kv","trTeachers","trStudents","trCourses","trAttendance"],
        "readwrite");
      /* 午托样例：学生 + 记录 + 头像 + kv */
      tx.objectStore("students").put({id:"stu-up1", name:"升级样例生", gradeClass:"三年级1班", createdAt:1000, updatedAt:1000});
      tx.objectStore("records").put({key:"2026-10-20|lunch|stu-up1", date:"2026-10-20", slot:"lunch",
                                     studentId:"stu-up1", status:"dao", updatedAt:1000});
      tx.objectStore("avatars").put({id:"stu-up1", data:"data:image/png;base64,iVBORw0KGgo=", updatedAt:1000});
      tx.objectStore("kv").put({probe:"upgrade-v2"}, "upgrade-probe");
      /* 培训样例：老师 / 学生 / 课程 / 签到（旧 subject 文本，无任何科目档案） */
      tx.objectStore("trStudents").put({id:"tst-up1", name:"培训样例生", gradeClass:"五年级1班", active:true, createdAt:1000, updatedAt:1000});
      tx.objectStore("trTeachers").put({id:"tch-up1", name:"升级样例师", subject:"书法", active:true, createdAt:1000, updatedAt:1000});
      tx.objectStore("trCourses").put({id:"crs-up1", date:"2026-10-20", startTime:"09:00", endTime:"10:00",
        teacherId:"tch-up1", subject:"书法", title:"升级样例课", studentIds:["tst-up1"], createdAt:1000, updatedAt:1000});
      tx.objectStore("trAttendance").put({key:"crs-up1|tst-up1", courseId:"crs-up1", studentId:"tst-up1",
                                          status:"present", updatedAt:1000});
      tx.oncomplete = () => { db.close(); res(); };
      tx.onerror = () => rej(tx.error || new Error("v2 写样例失败"));
    };
    req.onerror = () => rej(req.error);
  });
  ok("v2 库建库并写入八张表样例成功", true);
  return {checks};
})()
"""

JS_AFTER_UPGRADE = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  await T.ready();

  /* 守卫：升级后的页面必须也是新版迁移代码 */
  ok("升级后页面加载的是新版迁移代码",
     String(X.trMigrateSubjects).indexOf("trTxt(c.subjectName)") >= 0);

  /* 库结构：版本 3，trSubjects 新增，原有八张表原封不动 */
  const db = await new Promise((res, rej) => {
    const r = indexedDB.open("wudian-db");
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  eq("升级后库版本为 3", db.version, 3);
  const stores = Array.from(db.objectStoreNames);
  ok("升级后新增 trSubjects", stores.indexOf("trSubjects") >= 0, stores.join(","));
  const V2 = ["students","records","avatars","kv","trTeachers","trStudents","trCourses","trAttendance"];
  ok("原有八张表一条不少", V2.every(s => stores.indexOf(s) >= 0), stores.join(","));
  db.close();

  /* 原样例逐条读回：证明升级没有重建/清空任何表 */
  const readAll = store => new Promise((res, rej) => {
    const r = indexedDB.open("wudian-db");
    r.onsuccess = () => {
      const d = r.result;
      const q = d.transaction(store, "readonly").objectStore(store).getAll();
      q.onsuccess = () => { d.close(); res(q.result || []); };
      q.onerror = () => { d.close(); rej(q.error); };
    };
    r.onerror = () => rej(r.error);
  });
  const stu = await readAll("students");
  eq("午托学生表未重建（仍 1 条）", stu.length, 1);
  eq("午托学生样例一字未动", (stu[0]||{}).name, "升级样例生");
  const rec = await readAll("records");
  eq("午托记录样例仍在", rec.filter(x => x.key === "2026-10-20|lunch|stu-up1").length, 1);
  const ava = await readAll("avatars");
  eq("头像样例仍在", ava.filter(x => x.id === "stu-up1").length, 1);
  const kv = await readAll("kv");
  eq("kv 样例仍在", kv.filter(x => x && x.probe === "upgrade-v2").length, 1);

  /* 培训侧：样例一条不少，旧文本自动迁移 */
  const t = T.getTeacher("tch-up1");
  ok("培训老师样例仍在", !!t && t.name === "升级样例师", JSON.stringify(t));
  const shufa = T.listSubjects({q:"书法"})[0];
  ok("老师旧文本迁移出书法科目", !!shufa);
  ok("老师补 subjectIds 指向书法", !!t && !!shufa && t.subjectIds.indexOf(shufa.id) >= 0,
     JSON.stringify(t && t.subjectIds));
  const tst = T.listStudents({q:"培训样例生"})[0];
  ok("培训学生样例仍在", !!tst);
  const c = T.getCourse("crs-up1");
  ok("培训课程样例仍在", !!c && c.title === "升级样例课");
  ok("旧课程补 subjectId", !!c && !!shufa && c.subjectId === shufa.id, c && c.subjectId);
  eq("旧课程补名称快照", (c||{}).subjectName, "书法");
  const att = T.listAttendance("crs-up1");
  eq("培训签到样例仍在", att.length, 1);
  eq("签到状态保留 present", (att[0]||{}).status, "present");
  eq("升级只迁出 1 个科目", X.trCounts().subjects, 1);

  /* 幂等：reload 不重复建科目、不丢关联 */
  await T.reload();
  eq("reload 后科目仍 1 个", X.trCounts().subjects, 1);
  ok("reload 后课程科目关联仍在", (T.getCourse("crs-up1")||{}).subjectId === (shufa||{}).id);
  ok("页面无乱码字符", document.documentElement.textContent.indexOf("\uFFFD") < 0);
  return {checks, counts: X.trCounts()};
})()
"""


def run_upgrade_phase():
    """真实 v2→v3 升级：带时间戳的全新 profile（不删旧目录，保证 origin 里
    绝无旧库），先在 404 页同 origin 建 v2 库，关闭连接后加载当前 index.html
    触发升级。"""
    results = {"checks": [], "fatal": None, "screenshots": []}
    page = None
    profile = os.path.join(ROOT, "tests",
                           "_e2e_v15_upgrade_" + time.strftime("%Y%m%d-%H%M%S"))
    try:
        page = launch(profile=profile, fresh=False)
        page.send("Page.navigate",
                  {"url": "http://127.0.0.1:%d/tests/__no_such_v2_page__" % HTTP_PORT})
        time.sleep(0.8)
        page.js(JS_CLEAN_ENV)
        r0 = page.js(JS_MAKE_V2)
        results["checks"].extend(r0["checks"])
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)
        print("      升级触发完成，校验库结构与样例数据")
        r1 = page.js(JS_AFTER_UPGRADE)
        results["checks"].extend(r1["checks"])
        results["counts"] = r1.get("counts")
        results["screenshots"].append(page.shot("v15-upgrade-v2-to-v3"))
    except Exception as e:  # noqa: BLE001
        results["fatal"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if page:
            page.close()
    return results


# --------------------------------------------------------------------------
# 第 1 段：科目数据层 / 迁移 / 备份兼容
# --------------------------------------------------------------------------
JS_MAIN = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const fails = (n, fn, kw) => fn().then(() => checks.push({name:n, ok:false, detail:"本应抛错却成功了"}) )
                             .catch(e => {
                               const m = String(e.message || e);
                               checks.push({name:n, ok: !kw || m.indexOf(kw) >= 0, detail:m});
                             });

  await T.ready();

  /* 守卫：确认页面跑的是本轮新版迁移代码（旧版会把课程文本建档、覆盖快照） */
  ok("页面加载的是新版迁移代码", String(X.trMigrateSubjects).indexOf("trTxt(c.subjectName)") >= 0,
     String(X.trMigrateSubjects).slice(0, 120));

  /* ---------- 0. 建库与 API ---------- */
  ok("科目 API 齐全",
     ["listSubjects","activeSubjects","getSubject","createSubject","updateSubject",
      "setSubjectActive","removeSubject","subjectName"].every(k => typeof T[k] === "function"));
  const db = await new Promise((res, rej) => { const r = indexedDB.open("wudian-db"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const stores = Array.from(db.objectStoreNames);
  ok("新库存在 trSubjects", stores.indexOf("trSubjects") >= 0, stores.join(","));
  ok("原七张表一个不少",
     ["students","records","avatars","kv","trTeachers","trStudents","trCourses","trAttendance"]
       .every(s => stores.indexOf(s) >= 0), stores.join(","));

  /* 从干净状态开始（五张培训表全清，不碰午托） */
  await T.restoreSnapshot({teachers:[], students:[], courses:[], attendance:[]});
  eq("清空后科目 0", T.listSubjects().length, 0);

  /* ---------- 1. 科目 CRUD ---------- */
  const m1 = await T.createSubject({name:"数学"});
  ok("科目 id 用 subj- 前缀", String(m1.id).indexOf("subj-") === 0, m1.id);
  eq("科目名去首尾空格", m1.name, "数学");
  await fails("同名科目（多余空白）被拒绝", () => T.createSubject({name:"  数学  "}), "已存在同名科目");
  const e1 = await T.createSubject({name:"英语", note:"五年级"});
  const sci = await T.createSubject({name:"Science"});
  await fails("英文名大小写判重", () => T.createSubject({name:"science"}), "已存在同名科目");
  await fails("科目名称必填", () => T.createSubject({name:"   "}), "必填");
  const sp = await T.createSubject({name:"多  空格   科目"});
  eq("连续空白并作一个空格", sp.name, "多 空格 科目");
  eq("科目列表 4 条", T.listSubjects().length, 4);
  await T.removeSubject(sci.id);   /* 清理英文样例，后续断言按 3 个科目口径 */
  ok("按名称稳定排序", JSON.stringify(T.listSubjects().map(s => s.name)) === JSON.stringify(["多 空格 科目","数学","英语"]),
     T.listSubjects().map(s => s.name).join("/"));
  eq("搜索命中 1 条", T.listSubjects({q:"英语"}).length, 1);
  eq("备注也参与搜索", T.listSubjects({q:"五年级"}).length, 1);
  eq("搜不到为空", T.listSubjects({q:"查无"}).length, 0);
  eq("activeSubjects 全启用", T.activeSubjects().length, 3);

  await T.setSubjectActive(e1.id, false);
  eq("停用后启用名单 2", T.activeSubjects().length, 2);
  eq("activeOnly 过滤停用项", T.listSubjects({activeOnly:true}).length, 2);
  eq("全量列表仍有 3 条", T.listSubjects().length, 3);
  eq("停用项仍可读", T.getSubject(e1.id).active, false);
  await T.setSubjectActive(e1.id, true);
  eq("重新启用回 3", T.activeSubjects().length, 3);

  const m1b = await T.updateSubject(m1.id, {name:"数学思维", note:"改名"});
  eq("科目改名生效", m1b.name, "数学思维");
  eq("原 createdAt 保留", m1b.createdAt, m1.createdAt);
  await fails("改名撞已有科目被拒绝", () => T.updateSubject(m1.id, {name:"英语"}), "已存在同名科目");
  await T.updateSubject(m1.id, {name:"数学"});

  /* ---------- 2. 老师多科目 ---------- */
  const math = T.listSubjects({q:"数学"})[0];
  const eng = T.listSubjects({q:"英语"})[0];
  const t1 = await T.createTeacher({name:"钱老师", subjectIds:[math.id, eng.id], subject:"数学"});
  eq("老师挂两个科目", T.getTeacher(t1.id).subjectIds.length, 2);
  ok("老师 subjectIds 含数学", T.getTeacher(t1.id).subjectIds.indexOf(math.id) >= 0);
  ok("老师 subjectIds 含英语", T.getTeacher(t1.id).subjectIds.indexOf(eng.id) >= 0);
  /* 返回的是数组副本，外面改不动内部状态 */
  const g1 = T.getTeacher(t1.id);
  g1.subjectIds.push("fake");
  eq("getTeacher 返回数组副本", T.getTeacher(t1.id).subjectIds.length, 2);
  const l1 = T.listTeachers()[0];
  l1.subjectIds.push("fake");
  eq("listTeachers 返回数组副本", T.getTeacher(t1.id).subjectIds.length, 2);
  /* 旧 subject 文本兼容：只给文本，自动挂上同名科目 */
  const t2 = await T.createTeacher({name:"孙老师", subject:"英语"});
  ok("文本科目自动挂 subjectIds", T.getTeacher(t2.id).subjectIds.indexOf(eng.id) >= 0,
     JSON.stringify(T.getTeacher(t2.id).subjectIds));
  eq("旧 subject 文本保留", T.getTeacher(t2.id).subject, "英语");
  /* 显式列表以调用方为准 */
  const t3 = await T.createTeacher({name:"周老师"});
  await T.updateTeacher(t3.id, {subjectIds:[math.id]});
  eq("显式更新 subjectIds", JSON.stringify(T.getTeacher(t3.id).subjectIds), JSON.stringify([math.id]));
  /* 搜索能按科目名命中：钱老师挂着英语、孙老师文本科目也是英语 */
  eq("按科目档案名搜到老师", T.listTeachers({q:"英语"}).length, 2);

  /* ---------- 3. 课程 subjectId + 名称快照 ---------- */
  const s1 = await T.createStudent({name:"测试生甲", gradeClass:"五年级1班"});
  const D = "2026-11-01";
  const c1 = await T.createCourse({date:D, startTime:"16:00", endTime:"17:00", teacherId:t1.id,
                                   subjectId:math.id, title:"ID 建课", studentIds:[s1.id]});
  eq("课程落 subjectId", T.getCourse(c1.id).subjectId, math.id);
  eq("课程落名称快照", T.getCourse(c1.id).subjectName, "数学");
  eq("旧 subject 字段兼容写入", T.getCourse(c1.id).subject, "数学");
  await fails("不存在的 subjectId 被拒绝", () => T.createCourse({date:D, startTime:"18:00", endTime:"19:00",
                                   teacherId:t1.id, subjectId:"subj-nope", studentIds:[s1.id]}), "科目不存在");
  const c2 = await T.createCourse({date:D, startTime:"18:00", endTime:"19:00", teacherId:t2.id,
                                   subject:"英语", title:"文本建课", studentIds:[s1.id]});
  ok("旧文本建课自动挂科目", T.getCourse(c2.id).subjectId === eng.id, T.getCourse(c2.id).subjectId);
  eq("文本建课名称快照", T.getCourse(c2.id).subjectName, "英语");
  const noSub = await T.createCourse({date:D, startTime:"20:00", endTime:"21:00", teacherId:t1.id,
                                      subject:"冷门科目", title:"无档案文本", studentIds:[s1.id]});
  eq("对不上档案的文本 subjectId 为空", T.getCourse(noSub.id).subjectId, "");
  eq("对不上档案的文本保留", T.getCourse(noSub.id).subject, "冷门科目");

  /* 科目改名后，未编辑的课程名称快照保持历史原貌 */
  await T.updateSubject(math.id, {name:"数学培优"});
  eq("改名不改历史课程快照", T.getCourse(c1.id).subjectName, "数学");
  await T.updateSubject(math.id, {name:"数学"});

  /* ---------- 4. 删除保护 ---------- */
  await fails("被老师引用的科目不得硬删", () => T.removeSubject(math.id), "停用");
  await fails("被课程引用的科目不得硬删", () => T.removeSubject(eng.id), "停用");
  const tmp = await T.createSubject({name:"临时科目"});
  const rm = await T.removeSubject(tmp.id);
  ok("未被引用的科目可删除", !!rm.removed);
  eq("删除后科目数 3", T.listSubjects().length, 3);

  /* ---------- 5. v1.4 旧快照恢复：自动补科目 + 关联 + 幂等 ---------- */
  const v14snap = {
    teachers: [
      {id:"tch-old1", name:"旧老师甲", subject:"数学", active:true, createdAt:1000, updatedAt:1000},
      {id:"tch-old2", name:"旧老师乙", subject:"英语", active:true, createdAt:1000, updatedAt:1000},
      {id:"tch-old3", name:"旧老师丙", subject:"奥数", active:true, createdAt:1000, updatedAt:1000}
    ],
    students: [{id:"tst-old1", name:"旧学生甲", gradeClass:"四年级2班", active:true, createdAt:1000, updatedAt:1000}],
    courses: [
      {id:"crs-old1", date:"2026-10-01", startTime:"09:00", endTime:"10:00", teacherId:"tch-old1",
       subject:"数学", title:"旧课一", studentIds:["tst-old1"], createdAt:1000, updatedAt:1000},
      {id:"crs-old2", date:"2026-10-02", startTime:"09:00", endTime:"10:00", teacherId:"tch-old2",
       subject:"英语", title:"旧课二", studentIds:["tst-old1"], createdAt:1000, updatedAt:1000},
      {id:"crs-old3", date:"2026-10-03", startTime:"09:00", endTime:"10:00", teacherId:"tch-old3",
       subject:"奥数", title:"旧课三", studentIds:[], createdAt:1000, updatedAt:1000},
      {id:"crs-old4", date:"2026-10-04", startTime:"09:00", endTime:"10:00", teacherId:"tch-old3",
       subject:"", subjectId:"subj-gone-1", title:"旧课四（无科目文本）", studentIds:[], createdAt:1000, updatedAt:1000}
    ],
    attendance: [{key:"crs-old1|tst-old1", courseId:"crs-old1", studentId:"tst-old1", status:"present", updatedAt:1000}]
  };
  const back = await X.restoreTrainingSnapshot(v14snap);
  eq("恢复老师 3", back.teachers, 3);
  eq("恢复学生 1", back.students, 1);
  eq("恢复课程 4", back.courses, 4);
  eq("恢复明细 1", back.attendance, 1);
  eq("旧文本迁移出 3 个科目", back.subjects, 3);   /* 数学 / 英语 / 奥数，同名只录一条 */
  const old1 = T.getTeacher("tch-old1");
  const mathSub = T.listSubjects({q:"数学"})[0];
  const engSub = T.listSubjects({q:"英语"})[0];
  const aoSub = T.listSubjects({q:"奥数"})[0];
  ok("旧老师补 subjectIds（数学）", !!old1 && old1.subjectIds.indexOf(mathSub.id) >= 0,
     JSON.stringify(old1 && old1.subjectIds));
  const old3 = T.getTeacher("tch-old3");
  ok("旧老师补 subjectIds（奥数）", !!old3 && old3.subjectIds.indexOf(aoSub.id) >= 0);
  const oc1 = T.getCourse("crs-old1");
  ok("旧课程补 subjectId", oc1.subjectId === mathSub.id, oc1.subjectId);
  eq("旧课程补名称快照", oc1.subjectName, "数学");
  const oc3 = T.getCourse("crs-old3");
  ok("旧课程补 subjectId（奥数）", oc3.subjectId === aoSub.id);

  /* 无科目文本 + 悬空 subjectId 的历史课程：迁移清掉悬空 ID，
     显示兜底不出现 undefined（UI 侧是 c.subject || "未填科目"） */
  const oc4 = T.getCourse("crs-old4");
  eq("悬空 subjectId 被清空", oc4.subjectId || "", "");
  eq("无科目文本的课程原文仍为空串", oc4.subject, "");
  ok("无文本课程显示兜底不出现 undefined",
     String(oc4.subject) !== "undefined" && String(oc4.subjectName) !== "undefined" &&
     (oc4.subject || "未填科目") === "未填科目",
     "subject=" + JSON.stringify(oc4.subject) + " subjectName=" + JSON.stringify(oc4.subjectName));
  eq("无文本课程不产生新科目：科目仍 3 个", T.listSubjects().length, 3);

  /* 同名科目录一条：旧老师甲/丙都写"数学"，只有一个数学科目 */
  eq("数学科目只录一条", T.listSubjects({q:"数学"}).length, 1);

  /* 幂等：reload 后科目数不变、关联不重复 */
  await T.reload();
  eq("reload 后科目仍 3 个", T.listSubjects().length, 3);
  eq("reload 后老师 subjectIds 不重复", T.getTeacher("tch-old1").subjectIds.length, 1);
  ok("reload 后课程关联还在", T.getCourse("crs-old1").subjectId === mathSub.id);
  eq("reload 后悬空 subjectId 保持清空", T.getCourse("crs-old4").subjectId || "", "");

  /* 科目改名后 reload：历史课程必须保留旧名称快照，不得被迁移用现名覆盖。
     用「无旧科目文本」的课程验证：课程只带 subjectId + subjectName 快照 */
  const art = await T.createSubject({name:"美术"});
  const cArt = await T.createCourse({date:"2026-10-05", startTime:"14:00", endTime:"15:00",
                                     teacherId:"tch-old3", subjectId:art.id,
                                     title:"快照保留课", studentIds:[]});
  eq("建课落名称快照", T.getCourse(cArt.id).subjectName, "美术");
  await T.updateCourse(cArt.id, {subject:""});   /* 清掉旧文本，只剩 ID + 快照 */
  eq("清空文本后快照仍在", T.getCourse(cArt.id).subjectName, "美术");
  await T.updateSubject(art.id, {name:"美术培优"});
  await T.reload();
  eq("科目改名后 reload 历史课程快照不变", T.getCourse(cArt.id).subjectName, "美术");
  eq("改名后 reload 课程仍指向原科目", T.getCourse(cArt.id).subjectId, art.id);
  eq("改名后 reload 无旧文本不回建旧名档案", T.listSubjects({q:"美术"}).length, 1);
  eq("改名后 reload 老课程快照同样保留", T.getCourse("crs-old1").subjectName, "数学");

  /* 清理美术样例，后续快照口径回到 3 科目 */
  await T.removeCourse(cArt.id);
  await T.removeSubject(art.id);
  eq("清理后科目仍 3 个", T.listSubjects().length, 3);

  /* ---------- 6. v1.5 快照：schema 2 + subjects 段 + 完整恢复 ---------- */
  const snap = X.trainingSnapshot();
  eq("快照 schema 2", snap.schema, 2);
  ok("快照含 subjects 段", Array.isArray(snap.subjects) && snap.subjects.length === 3,
     JSON.stringify((snap.subjects||[]).map(s=>s.name)));
  ok("快照老师带 subjectIds 数组", snap.teachers.every(t => Array.isArray(t.subjectIds)));
  const back2 = await X.restoreTrainingSnapshot(snap);
  eq("v1.5 快照恢复科目 3", back2.subjects, 3);
  eq("v1.5 快照恢复老师 3", back2.teachers, 3);
  eq("v1.5 快照恢复课程 4", back2.courses, 4);
  eq("v1.5 快照恢复明细 1", back2.attendance, 1);
  ok("v1.5 恢复后老师关联完整", T.getTeacher("tch-old1").subjectIds.indexOf(mathSub.id) >= 0);
  ok("v1.5 恢复后课程关联完整", T.getCourse("crs-old1").subjectId === mathSub.id);

  /* subjectName(id, fallback) */
  eq("subjectName 命中档案", T.subjectName(mathSub.id, "兜底"), "数学");
  eq("subjectName 未命中走兜底", T.subjectName("subj-nope", "兜底文本"), "兜底文本");
  eq("subjectName 空参走兜底", T.subjectName("", "旧文本"), "旧文本");

  /* trCounts 带科目数量 */
  eq("trCounts 统计科目数", X.trCounts().subjects, T.listSubjects().length);

  /* ---------- 7. 页面无乱码 ---------- */
  ok("页面无乱码字符", document.documentElement.textContent.indexOf("\uFFFD") < 0);

  return {checks, counts: X.trCounts()};
})()
"""

# --------------------------------------------------------------------------
# 第 2 段：刷新后持久化
# --------------------------------------------------------------------------
JS_AFTER_RELOAD = r"""
(async () => {
  const T = window.Training, X = window.__test;
  await T.ready();
  const c = X.trCounts();
  const old1 = T.getTeacher("tch-old1");
  const mathSub = T.listSubjects({q:"数学"})[0];
  return {checks: [
    {name:"刷新后科目仍在", ok:c.subjects === 3, detail:"实际 " + c.subjects},
    {name:"刷新后老师仍在", ok:c.teachers === 3, detail:"实际 " + c.teachers},
    {name:"刷新后课程仍在", ok:c.courses === 4, detail:"实际 " + c.courses},
    {name:"刷新后签到明细仍在", ok:c.attendance === 1, detail:"实际 " + c.attendance},
    {name:"刷新后老师科目关联仍在", ok:!!old1 && !!mathSub && old1.subjectIds.indexOf(mathSub.id) >= 0,
     detail: old1 ? JSON.stringify(old1.subjectIds) : "老师丢失"},
    {name:"刷新后课程科目关联仍在",
     ok:(T.getCourse("crs-old1")||{}).subjectId === (mathSub||{}).id,
     detail: JSON.stringify((T.getCourse("crs-old1")||{}).subjectId)}
  ]};
})()
"""


def main():
    srv = serve()
    page = None
    results = {"checks": [], "fatal": None, "screenshots": []}
    try:
        print("[0/4] 真实 IndexedDB v2→v3 升级（独立 profile，先建 v2 再加载现版页面）")
        up = run_upgrade_phase()
        results["checks"].extend(up["checks"])
        results["screenshots"].extend(up.get("screenshots", []))
        if up["fatal"]:
            raise RuntimeError("升级阶段失败：" + up["fatal"])

        page = launch()
        page.send("Page.navigate",
                  {"url": "http://127.0.0.1:%d/tests/__no_such_v15_page__" % HTTP_PORT})
        time.sleep(0.6)
        page.js(JS_CLEAN_ENV)   # 同源注销 SW + 清 CacheStorage，防旧外壳缓存劫持导航
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)
        print("[1/4] 科目数据层 / 老师多科目 / 课程快照 / 删除保护 / 迁移 / 备份")
        main_res = page.js(JS_MAIN)
        results["checks"].extend(main_res["checks"])
        results["counts"] = main_res.get("counts")
        results["screenshots"].append(page.shot("v15-data-layer"))

        print("[2/4] 刷新页面，验证科目与关联持久化")
        page.send("Page.reload")
        time.sleep(1.0)
        page.wait_ready()
        r2 = page.js(JS_AFTER_RELOAD)
        results["checks"].extend(r2["checks"])

        print("[3/4] 收尾")
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
