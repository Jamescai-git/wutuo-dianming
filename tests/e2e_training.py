# -*- coding: utf-8 -*-
"""午托点名 v1.4 —— 培训数据层与签到界面端到端自测

真机（无头 Edge + CDP）验证：
  1. 培训四张表建库、增删改查、停用、搜索
  2. 同一天同时段多老师 / 同老师多节 / 同学生多节
  3. 签到明细（出勤 / 迟到 / 请假）写入、幂等、非法状态拒绝
  4. 与午托数据完全隔离（两个方向都查）
  5. 刷新后数据仍在（IndexedDB 持久化）
  6. JSON 全量备份 → 清空 → 恢复，四类数据数量与关联一致

用法（本机）：
  C:\\Users\\James\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\e2e_training.py
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
HTTP_PORT = 8099
CDP_PORT = 9444
PROFILE = os.path.join(ROOT, "tests", "_e2e_profile")
SHOT_DIR = os.path.join(ROOT, "tests", "screenshots")
REPORT = os.path.join(ROOT, "tests", "last-run.json")


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


# 加载 index.html 前在同源页面上执行：注销 Service Worker + 清 CacheStorage，
# 防止 profile 残留的旧版外壳缓存让页面跑旧代码（profile 不做删除，见 launch）
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
    # 陈旧 profile 的 SW/缓存污染由 main() 里导航前的 JS_CLEAN_ENV 兜底。
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


# --------------------------------------------------------------------------
# 第 1 段：主流程
# --------------------------------------------------------------------------
JS_MAIN = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fails = (n, fn) => fn().then(() => checks.push({name:n, ok:false, detail:"本应抛错却成功了"}) )
                             .catch(e => checks.push({name:n, ok:true, detail:String(e.message||e)}));

  await T.ready();
  /* v1.5：两套模式——测试统一从午托模式开始（profile 残留偏好也归位） */
  if (X.state.mode !== "wudian") { X.switchMode("wudian"); await sleep(300); }
  ok("window.Training 暴露完整 API",
     ["createTeacher","updateTeacher","setTeacherActive","listTeachers","activeTeachers",
      "createStudent","updateStudent","setStudentActive","listStudents",
      "createCourse","getCourse","updateCourse","removeCourse","listCourses","coursesOnDate",
      "listAttendance","setAttendance","setAttendanceBulk","removeAttendance",
      "teacherCourseCounts","snapshot","restoreSnapshot"].every(k => typeof T[k] === "function"));

  const db = await new Promise((res, rej) => { const r = indexedDB.open("wudian-db"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const stores = Array.from(db.objectStoreNames);
  ok("四张培训表已建库", ["trTeachers","trStudents","trCourses","trAttendance"].every(s => stores.indexOf(s) >= 0), stores.join(","));
  ok("午托三张表仍在", ["students","records","avatars"].every(s => stores.indexOf(s) >= 0), stores.join(","));
  const countStore = (name) => new Promise((res, rej) => {
    const r = db.transaction(name, "readonly").objectStore(name).count();
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  eq("学生表里没有 tst- 前缀的培训数据", (await countStore("students")), X.state.students.length);

  /* 从干净状态开始（只清培训，不碰午托） */
  await T.restoreSnapshot({teachers:[], students:[], courses:[], attendance:[]});
  eq("清空后培训老师 0", T.listTeachers().length, 0);

  /* ---------- 1. 建档：2 老师 3 学生 ---------- */
  const t1 = await T.createTeacher({name:"王老师", subject:"数学", contact:"13800000001", note:"周三晚"});
  const t2 = await T.createTeacher({name:"李老师", subject:"英语"});
  eq("老师 2 位", T.listTeachers().length, 2);
  await fails("同科同名老师被拒绝", () => T.createTeacher({name:"王老师", subject:"数学"}));
  await fails("老师缺姓名被拒绝", () => T.createTeacher({name:"  ", subject:"物理"}));

  const s1 = await T.createStudent({name:"陈小明", gradeClass:"五年级2班", contact:"13900000001"});
  const s2 = await T.createStudent({name:"林小美", gradeClass:"五年级1班"});
  const s3 = await T.createStudent({name:"周小刚", gradeClass:"六年级3班", note:"试听"});
  eq("培训学生 3 名", T.listStudents().length, 3);
  await fails("同名同班学生被拒绝", () => T.createStudent({name:"陈小明", gradeClass:"五年级2班"}));

  /* ---------- 2. 编辑 / 停用 / 搜索 ---------- */
  const t1b = await T.updateTeacher(t1.id, {subject:"数学（奥数）", contact:"13800000002"});
  eq("老师编辑生效", t1b.subject + "|" + t1b.contact, "数学（奥数）|13800000002");
  eq("老师原始创建时间保留", t1b.createdAt, t1.createdAt);
  await T.setStudentActive(s3.id, false);
  eq("停用后启用名单 2 人", T.activeStudents().length, 2);
  eq("全量名单仍有 3 人", T.listStudents().length, 3);
  eq("停用标记可读", T.getStudent(s3.id).active, false);
  eq("按班级搜索命中", T.listStudents({q:"五年级"}).length, 2);
  eq("按姓名搜索命中", T.listStudents({q:"林小美"}).length, 1);
  eq("按科目搜索命中", T.listTeachers({q:"英语"}).length, 1);
  eq("搜不到时为空", T.listStudents({q:"查无此人"}).length, 0);
  await T.setStudentActive(s3.id, true);
  eq("重新启用回到 3 人", T.activeStudents().length, 3);

  /* ---------- 3. 课程：同日同时段多老师 + 同老师多节 ---------- */
  const D = "2026-10-23";
  const c1 = await T.createCourse({date:D, startTime:"16:00", endTime:"17:30", teacherId:t1.id,
                                   subject:"数学", title:"分数应用题", studentIds:[s1.id, s2.id]});
  const c2 = await T.createCourse({date:D, startTime:"16:00", endTime:"17:30", teacherId:t2.id,
                                   subject:"英语", title:"Unit 3 复习", studentIds:[s2.id, s3.id]});
  const c3 = await T.createCourse({date:D, startTime:"19:00", endTime:"20:30", teacherId:t1.id,
                                   subject:"数学", title:"晚间提高班", studentIds:[s1.id]});
  eq("同日 3 节课程", T.coursesOnDate(D).length, 3);
  eq("同时段两个老师各一节", T.listCourses({date:D, from:D, to:D, teacherId:t2.id}).length, 1);
  eq("王老师同日 2 节", T.listCourses({date:D, teacherId:t1.id}).length, 2);
  eq("林小美同日 2 节", T.listCourses({date:D, studentId:s2.id}).length, 2);
  await fails("缺老师被拒绝", () => T.createCourse({date:D, teacherId:""}));
  await fails("结束早于开始被拒绝", () => T.createCourse({date:D, startTime:"18:00", endTime:"17:00", teacherId:t1.id}));

  /* ---------- 4. 签到明细 ---------- */
  await T.setAttendanceBulk(c1.id, [{studentId:s1.id, status:"present"}, {studentId:s2.id, status:"late"}]);
  await T.setAttendance(c2.id, s2.id, "present");
  eq("第 1 节 2 条明细", T.listAttendance(c1.id).length, 2);
  eq("同一学生同日在第 2 节另有明细", T.listAttendance(c2.id).length, 1);
  const map1 = T.attendanceMap(c1.id);
  eq("明细状态正确", map1[s1.id] + "/" + map1[s2.id], "present/late");
  const again = await T.setAttendance(c1.id, s1.id, "present");
  eq("同状态写入幂等", again.changed, false);
  await T.setAttendance(c1.id, s1.id, "leave");
  eq("改判为请假", T.attendanceMap(c1.id)[s1.id], "leave");
  await T.setAttendance(c1.id, s1.id, "present");
  await fails("非法状态被拒绝", () => T.setAttendance(c1.id, s1.id, "absent"));
  await fails("不在课上的学生被拒绝", () => T.setAttendance(c3.id, s2.id, "present"));

  /* ---------- 5. 上课次数口径（至少一名学生） ---------- */
  const c4 = await T.createCourse({date:"2026-10-24", startTime:"10:00", endTime:"11:00", teacherId:t2.id, subject:"英语", title:"空课"});
  const cnt = T.teacherCourseCounts(), cntAll = T.teacherCourseCounts({includeEmpty:true});
  eq("王老师计 2 节", cnt[t1.id], 2);
  eq("李老师只算有学生的 1 节", cnt[t2.id], 1);
  eq("含空课时李老师 2 节", cntAll[t2.id], 2);

  /* ---------- 6. 与午托隔离（双向） ---------- */
  const beforeW = X.trCounts();
  await T.createStudent({name:"培训专用", gradeClass:"四年级1班"});
  const afterW = X.trCounts();
  eq("培训新增不改动午托学生数", afterW.wudianStudents, beforeW.wudianStudents);
  eq("培训新增不改动午托记录数", afterW.wudianRecords, beforeW.wudianRecords);
  eq("培训里没有午托学生", T.listStudents().filter(x => x.id.indexOf("tst-") !== 0).length, 0);

  /* 午托 UI 新增一名学生，培训名单不应变化 */
  document.querySelector('.tab[data-tab="roster"]').click();
  await sleep(260);
  document.querySelector("#addStu").click();
  await sleep(280);
  document.querySelector("#fName").value = "午托甲";
  document.querySelector("#fGroup").value = "三年级1班";
  document.querySelector("#saveStu").click();
  await sleep(600);
  if (X.state.mode !== "training") document.querySelector('#modeSeg button[data-mode="training"]').click();
  await sleep(320);
  eq("午托加人后午托学生 1 名", X.state.students.length, 1);
  eq("午托学生没进培训名单", T.listStudents({q:"午托甲"}).length, 0);
  eq("培训名单仍为 4 人", T.listStudents().length, 4);

  /* ---------- 7. 备份 / 恢复 ---------- */
  const json = await X.exportBackup();
  const obj = JSON.parse(json);
  ok("备份含 training 段", !!(obj.training && Array.isArray(obj.training.teachers)), Object.keys(obj.training || {}).join(","));
  eq("备份老师数", obj.training.teachers.length, 2);
  eq("备份培训学生数", obj.training.students.length, 4);
  eq("备份课程数", obj.training.courses.length, 4);
  eq("备份明细数", obj.training.attendance.length, 3);
  const trIds = new Set(obj.training.students.map(s => s.id));
  ok("备份课程里的学生都在培训学生里",
     obj.training.courses.every(c => c.studentIds.every(id => trIds.has(id))));
  ok("备份明细都指向存在的课程",
     obj.training.attendance.every(a => obj.training.courses.some(c => c.id === a.courseId)));
  ok("备份含午托学生", Array.isArray(obj.students) && obj.students.length === 1);

  /* 改动后再恢复，数量和关联要回到备份时 */
  await T.updateCourse(c1.id, {studentIds:[s1.id]});
  eq("移出学生后明细被清理", T.listAttendance(c1.id).length, 1);
  await T.removeCourse(c4.id);
  eq("删课后课程数 3", T.listCourses().length, 3);
  const back = await X.restoreTrainingSnapshot(obj.training);
  eq("恢复老师 2", back.teachers, 2);
  eq("恢复培训学生 4", back.students, 4);
  eq("恢复课程 4", back.courses, 4);
  eq("恢复明细 3", back.attendance, 3);
  eq("恢复后第 1 节明细回到 2 条", T.listAttendance(c1.id).length, 2);
  eq("恢复后午托学生仍在", X.state.students.length, 1);

  /* ---------- 8. 界面（v1.5：培训是独立模式，底部四项导航） ---------- */
  if (X.state.mode !== "training") document.querySelector('#modeSeg button[data-mode="training"]').click();
  await sleep(300);
  ok("培训底部恰好四项导航", document.querySelectorAll("#tabBar .tab").length === 4 &&
     ["signin","archives","stats","info"].every(t => !!document.querySelector(`#tabBar .tab[data-tab="${t}"]`)));
  ok("旧顶部五段导航已移除", !document.querySelector("#trSeg"));
  document.querySelector('.tab[data-tab="archives"]').click();
  await sleep(220);
  ok("档案内部三页签存在", !!document.querySelector("#trArchSeg") &&
     document.querySelectorAll("#trArchSeg button").length === 3);
  const rows = document.querySelectorAll("#view .row").length;
  eq("学生档案渲染 4 行", rows, 4);
  ok("中文正常显示", document.querySelector("#view").textContent.indexOf("陈小明") >= 0);
  const found = (T.listStudents({q:"陈小"})).length === 1;

  /* 搜索框真机输入 */
  const si = document.querySelector("#trSearch");
  si.value = "六年级";
  si.dispatchEvent(new Event("input", {bubbles:true}));
  await sleep(260);
  eq("界面搜索过滤到 1 行", document.querySelectorAll("#view .row").length, 1);

  /* 老师档案分段 */
  document.querySelector('#trArchSeg button[data-seg="teachers"]').click();
  await sleep(260);
  eq("老师档案 2 行", document.querySelectorAll("#view .row").length, 2);
  ok("老师行显示科目", document.querySelector("#view").textContent.indexOf("数学（奥数）") >= 0);

  /* 概况页（v1.5：底部直达） */
  document.querySelector('.tab[data-tab="info"]').click();
  await sleep(260);
  ok("概况页有上课次数", document.querySelector("#view").textContent.indexOf("老师上课次数") >= 0);

  /* 午托点名页仍可用（未被培训数据污染） */
  if (X.state.mode !== "wudian") document.querySelector('#modeSeg button[data-mode="wudian"]').click();
  await sleep(300);
  document.querySelector('.tab[data-tab="roll"]').click();
  await sleep(300);
  const cards = document.querySelectorAll("#grid .card").length;
  eq("午托点名卡片只有 1 张（午托甲）", cards, 1);
  ok("培训学生没出现在点名页", document.querySelector("#grid").textContent.indexOf("陈小明") < 0);

  if (X.state.mode !== "training") document.querySelector('#modeSeg button[data-mode="training"]').click();
  await sleep(300);
  document.querySelector('.tab[data-tab="archives"]').click();
  await sleep(220);
  document.querySelector('#trArchSeg button[data-seg="students"]').click();
  await sleep(200);

  return {checks, snapshot: X.trCounts(),
          ids: {t1:t1.id, s1:s1.id, c1:c1.id}};
})()
"""

# --------------------------------------------------------------------------
# 第 2 段：刷新后校验持久化
# --------------------------------------------------------------------------
JS_AFTER_RELOAD = r"""
(async () => {
  const T = window.Training, X = window.__test;
  await T.ready();
  const c = X.trCounts();
  return {counts: c, checks: [
    {name:"刷新后老师仍在", ok:c.teachers === 2, detail:"实际 " + c.teachers},
    {name:"刷新后培训学生仍在", ok:c.students === 4, detail:"实际 " + c.students},
    {name:"刷新后课程仍在", ok:c.courses === 4, detail:"实际 " + c.courses},
    {name:"刷新后签到明细仍在", ok:c.attendance === 3, detail:"实际 " + c.attendance},
    {name:"刷新后午托学生仍在", ok:c.wudianStudents === 1, detail:"实际 " + c.wudianStudents}
  ]};
})()
"""

JS_AFTER_RELOAD_COURSE = r"""
(async () => {
  const T = window.Training;
  await T.ready();
  const cs = T.listCourses({date:"2026-10-23"});
  const c1 = cs.filter(c => (c.studentIds||[]).length === 2 && c.startTime === "16:00" && c.title === "分数应用题")[0];
  return {checks: [
    {name:"刷新后同日 3 节可读", ok:cs.length === 3, detail:"实际 " + cs.length},
    {name:"刷新后明细关联还在", ok:!!c1 && T.listAttendance(c1.id).length === 2,
     detail: c1 ? T.listAttendance(c1.id).length : "未找到课程"},
    {name:"刷新后明细状态正确", ok:!!c1 && T.attendanceMap(c1.id)[c1.studentIds[0]] === "present"}
  ]};
})()
"""


# --------------------------------------------------------------------------
# 第 3 段：v1.4 培训签到 / 调课 / 统计 / 移动端界面
# --------------------------------------------------------------------------
JS_V14 = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const errText = async (fn) => {
    try { await fn(); return ""; }
    catch (e) { return String(e && e.message || e); }
  };

  await T.ready();
  eq("版本号为 1.5.0", X.appVersion, "1.5.0");
  ok("v1.4 测试钩子完整",
     ["trSaveSignin","trFindConflicts","trCourseStats","trStudentStats","trTeacherStats","trCsvText"]
       .every(k => typeof X[k] === "function"));

  const D = "2026-10-30";
  const t1 = T.listTeachers({q:"王老师"})[0];
  const t2 = T.listTeachers({q:"李老师"})[0];
  const s1 = T.listStudents({q:"陈小明"})[0];
  const s2 = T.listStudents({q:"林小美"})[0];
  const s3 = T.listStudents({q:"周小刚"})[0];
  const s4 = T.listStudents({q:"培训专用"})[0];
  ok("v1.4 测试档案齐全", !!(t1 && t2 && s1 && s2 && s3 && s4));

  const a = await X.trSaveSignin({
    date:D, startTime:"09:00", endTime:"10:00", teacherId:t1.id, subject:"数学", note:"v1.4 三状态",
    studentIds:[s1.id,s2.id,s3.id],
    statuses:{[s1.id]:"present", [s2.id]:"late", [s3.id]:"leave"}
  });
  const aStats = X.trCourseStats(a.id);
  eq("逐人三状态：出勤", aStats.present, 1);
  eq("逐人三状态：迟到", aStats.late, 1);
  eq("逐人三状态：请假", aStats.leave, 1);
  eq("逐人三状态：无遗漏", aStats.missing, 0);

  const countBeforeBad = T.listCourses({date:D}).length;
  const noSubErr = await errText(() => X.trSaveSignin({
    date:D, startTime:"15:00", endTime:"16:00", teacherId:t2.id,
    studentIds:[s2.id], statuses:{[s2.id]:"present"}
  }));
  ok("新建课程未选科目阻止提交", noSubErr.indexOf("请选择科目") >= 0, noSubErr);
  const missErr = await errText(() => X.trSaveSignin({
    date:D, startTime:"15:00", endTime:"16:00", teacherId:t2.id, subject:"英语",
    studentIds:[s2.id], statuses:{}
  }));
  ok("未标状态阻止提交", missErr.indexOf("未选择状态") >= 0, missErr);
  eq("未标状态不新增课程", T.listCourses({date:D}).length, countBeforeBad);

  const conflictErr = await errText(() => X.trSaveSignin({
    date:D, startTime:"09:30", endTime:"10:30", teacherId:t2.id, subject:"数学",
    studentIds:[s1.id], statuses:{[s1.id]:"present"}
  }));
  ok("学生重叠时段阻止提交", conflictErr.indexOf("时间冲突") >= 0, conflictErr);
  eq("冲突失败不新增课程", T.listCourses({date:D}).length, countBeforeBad);

  const b = await X.trSaveSignin({
    date:D, startTime:"09:00", endTime:"10:00", teacherId:t2.id, subject:"英语",
    studentIds:[s4.id], statuses:{[s4.id]:"present"}
  });
  const c = await X.trSaveSignin({
    date:D, startTime:"11:00", endTime:"12:00", teacherId:t1.id, subject:"数学",
    studentIds:[s1.id], statuses:{[s1.id]:"present"}
  });
  const d = await X.trSaveSignin({
    date:D, startTime:"13:00", endTime:"14:00", teacherId:t2.id, subject:"英语",
    studentIds:[s1.id], statuses:{[s1.id]:"late"}
  });
  eq("v1.4 同日共 4 节课", T.listCourses({date:D}).length, 4);
  eq("同一时段两位老师各有课", T.listCourses({date:D}).filter(x => x.startTime === "09:00").length, 2);
  eq("同一老师同日两节", T.listCourses({date:D, teacherId:t1.id}).length, 2);
  eq("同一学生同日三节", T.listCourses({date:D, studentId:s1.id}).length, 3);

  const moved = await X.trSaveSignin({
    courseId:c.id, date:D, startTime:"11:30", endTime:"12:30", teacherId:t1.id, subject:"数学",
    studentIds:[s1.id], statuses:{[s1.id]:"present"}
  });
  eq("调课时间已更新", moved.startTime, "11:30");
  const moveConflict = await errText(() => X.trSaveSignin({
    courseId:d.id, date:D, startTime:"09:30", endTime:"10:30", teacherId:t2.id, subject:"英语",
    studentIds:[s1.id], statuses:{[s1.id]:"late"}
  }));
  ok("调课遇冲突会阻止", moveConflict.indexOf("时间冲突") >= 0, moveConflict);
  eq("冲突调课不改原课程", T.getCourse(d.id).startTime, "13:00");

  const ss = X.trStudentStats(s1.id, {from:D,to:D});
  eq("学生统计上课次数", ss.total, 3);
  eq("学生统计出勤次数", ss.present, 2);
  eq("学生统计迟到次数", ss.late, 1);
  eq("学生统计请假次数", ss.leave, 0);
  const tt = X.trTeacherStats(t1.id, {from:D,to:D});
  eq("老师统计上课节数", tt.total, 2);
  eq("老师统计授课人次", tt.people, 4);
  eq("老师统计出勤人次", tt.present, 2);
  eq("老师统计迟到人次", tt.late, 1);
  eq("老师统计请假人次", tt.leave, 1);

  const csv = X.trCsvText([
    ["日期","开始","结束","老师","科目","学生","班级","状态"],
    [D,"09:00","10:00","王老师","数学","陈小明","五年级2班","出勤"]
  ]);
  ok("CSV 带 UTF-8 BOM", csv.charCodeAt(0) === 0xFEFF);
  ok("CSV 表头完整", csv.indexOf("日期,开始,结束,老师,科目,学生,班级,状态") > 0);
  ok("CSV 中文内容完整", csv.indexOf("陈小明,五年级2班,出勤") > 0);

  if (X.state.mode !== "training") document.querySelector('#modeSeg button[data-mode="training"]').click();
  await sleep(180);
  eq("培训页隐藏午托状态栏", getComputedStyle(document.querySelector("#brushBar")).display, "none");
  eq("培训页隐藏午托进度条", getComputedStyle(document.querySelector("#progBar")).display, "none");
  eq("培训页隐藏午托计数条", getComputedStyle(document.querySelector("#statsBar")).display, "none");
  const dp = document.querySelector("#datePick");
  dp.value = D;
  dp.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(180);
  X.state.trTab = "signin";
  X.renderTraining();
  await sleep(320);
  ok("签到页日期标题跟随所选日期", document.querySelector("#dateText").textContent.indexOf("10月30日") >= 0);
  eq("培训底部恰好四项", document.querySelectorAll("#tabBar .tab").length, 4);
  ok("培训底部无午托按钮", !document.querySelector('.tab[data-tab="roll"]') &&
     !document.querySelector('.tab[data-tab="roster"]') && !document.querySelector('.tab[data-tab="records"]'));
  ok("旧顶部五段导航已移除", !document.querySelector("#trSeg"));
  eq("签到页显示当日 4 节课", document.querySelectorAll(".trcourse").length, 4);
  ok("签到页显示具体时间段", document.querySelector("#view").textContent.indexOf("09:00-10:00") >= 0);
  ok("签到页显示老师与学生数", document.querySelector("#view").textContent.indexOf("王老师") >= 0 &&
     document.querySelector("#view").textContent.indexOf("3 名学生") >= 0);

  document.querySelector("#trNewCourse").click();
  await sleep(180);
  ok("签到弹层字段齐全", ["#trDate","#trStart","#trEnd","#trTeacher","#trPickList"]
     .every(q => !!document.querySelector("#sheet " + q)));
  const pick = document.querySelector("#sheet .trpick");
  pick.click();
  await sleep(80);
  ok("选学生后不自动标状态", pick.classList.contains("sel") &&
     pick.querySelectorAll("[data-st].on").length === 0);
  document.querySelector("#trDate").value = D;
  document.querySelector("#trStart").value = "16:00";
  document.querySelector("#trEnd").value = "17:00";
  document.querySelector("#trTeacher").value = t1.id;
  document.querySelector("#trTeacher").dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(80);
  /* t1 的科目文本在前面被改成了"数学（奥数）"，migrate 后任教科目即该条；
     下拉按"老师任教科目优先"只列任教科目，断言与之对齐 */
  const t1subUi = (t1.subjectIds || [])[0];
  const mathSubUi = t1subUi ? T.getSubject(t1subUi) : null;
  ok("签到弹层科目改为下拉且含老师任教科目",
     !!document.querySelector("#sheet select#trSubject") &&
     !!mathSubUi &&
     Array.from(document.querySelector("#sheet #trSubject").options).some(o => o.value === mathSubUi.id),
     mathSubUi ? "任教科目=" + mathSubUi.name : "老师无任教科目");
  document.querySelector("#trSubject").value = mathSubUi.id;
  const countBeforeUi = T.listCourses({date:D}).length;
  document.querySelector("#trCourseSave").click();
  await sleep(280);
  ok("前台未选状态时显示错误", !document.querySelector("#trErr").hidden &&
     document.querySelector("#trErr").textContent.indexOf("未选择状态") >= 0);
  eq("前台未选状态不保存", T.listCourses({date:D}).length, countBeforeUi);
  document.querySelector("#sheet [data-close]").click();
  await sleep(120);

  document.querySelector('.tab[data-tab="stats"]').click();
  await sleep(220);
  const ssel = document.querySelector("#trStatsStudent");
  ssel.value = s1.id;
  ssel.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(220);
  ok("学生统计界面有上课次数", document.querySelector("#view").textContent.indexOf("上课次数") >= 0);
  ok("学生统计界面有最近课程", document.querySelector("#view").textContent.indexOf("最近上课") >= 0);
  document.querySelector('#trStatsMode button[data-mode="teacher"]').click();
  await sleep(220);
  const tsel = document.querySelector("#trStatsTeacher");
  tsel.value = t1.id;
  tsel.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(220);
  ok("老师统计界面有节数与人次", document.querySelector("#view").textContent.indexOf("上课节数") >= 0 &&
     document.querySelector("#view").textContent.indexOf("授课人次") >= 0);

  X.state.trTab = "signin";
  X.renderTraining();
  await sleep(760);
  const noOverflow = document.documentElement.scrollWidth <= window.innerWidth + 1 &&
                     document.body.scrollWidth <= window.innerWidth + 1;
  ok("390px 手机无横向滚动", noOverflow,
     document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth);
  const pageText = document.documentElement.textContent;
  ok("移动端中文正常无乱码", pageText.indexOf("签到") >= 0 && pageText.indexOf("\uFFFD") < 0);
  const ext = Array.from(document.querySelectorAll("[src],[href]")).filter(el => {
    const v = el.getAttribute("src") || el.getAttribute("href") || "";
    return /^https?:\/\//i.test(v);
  });
  eq("页面无外部网络资源", ext.length, 0);

  return {checks, ids:{a:a.id,b:b.id,c:c.id,d:d.id,s1:s1.id,t1:t1.id}};
})()
"""

JS_V14_STATS_SHOT = r"""
(async () => {
  const X = window.__test;
  X.state.trTab = "stats";
  X.state.trStatsMode = "student";
  X.renderTraining();
  await new Promise(r => setTimeout(r, 300));
  window.scrollTo(0, 0);
  return true;
})()
"""

JS_V14_RELOAD = r"""
(async () => {
  const T = window.Training, X = window.__test;
  await T.ready();
  const D = "2026-10-30";
  const cs = T.listCourses({date:D});
  const c1 = cs.filter(c => c.startTime === "09:00" && c.teacherId === T.listTeachers({q:"王老师"})[0].id)[0];
  const moved = T.listCourses({date:D, teacherId:T.listTeachers({q:"王老师"})[0].id})
    .filter(c => c.startTime === "11:30")[0];
  return {checks:[
    {name:"刷新后 v1.4 四节课仍在", ok:cs.length === 4, detail:"实际 " + cs.length},
    {name:"刷新后 v1.4 六条明细仍在", ok:!!c1 && T.listAttendance(c1.id).length === 3, detail:c1 ? T.listAttendance(c1.id).length : "未找到首课"},
    {name:"刷新后调课结果仍在", ok:!!moved, detail:moved ? moved.startTime : "未找到调课"},
    {name:"刷新后版本仍为 1.5.0", ok:X.appVersion === "1.5.0", detail:X.appVersion}
  ]};
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
        page.js(JS_CLEAN_ENV)   # 同源注销 SW + 清 CacheStorage，防旧外壳缓存劫持导航
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)
        print("[1/5] 主流程：建档 / 课程 / 明细 / 隔离 / 备份恢复 / 界面")
        main_res = page.js(JS_MAIN)
        results["checks"].extend(main_res["checks"])
        results["snapshot"] = main_res.get("snapshot")
        results["screenshots"].append(page.shot("training-v1.4-students"))

        print("[2/5] 刷新页面，验证 v1.3 数据持久化")
        page.send("Page.reload")
        time.sleep(1.0)
        page.wait_ready()
        r2 = page.js(JS_AFTER_RELOAD)
        results["checks"].extend(r2["checks"])
        r3 = page.js(JS_AFTER_RELOAD_COURSE)
        results["checks"].extend(r3["checks"])

        print("[3/5] v1.4 签到 / 逐人状态 / 冲突 / 调课 / 统计 / 390px 界面")
        page.send("Emulation.setDeviceMetricsOverride", {
            "width": 390, "height": 844, "deviceScaleFactor": 1,
            "mobile": True, "screenWidth": 390, "screenHeight": 844
        })
        r4 = page.js(JS_V14)
        results["checks"].extend(r4["checks"])
        results["v14_ids"] = r4.get("ids")
        results["screenshots"].append(page.shot("training-v1.4-signin"))
        page.js(JS_V14_STATS_SHOT)
        results["screenshots"].append(page.shot("training-v1.4-stats"))

        print("[4/5] 刷新页面，验证 v1.4 持久化")
        page.send("Page.reload")
        time.sleep(1.0)
        page.wait_ready()
        r5 = page.js(JS_V14_RELOAD)
        results["checks"].extend(r5["checks"])

        print("[5/5] 收尾")
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
