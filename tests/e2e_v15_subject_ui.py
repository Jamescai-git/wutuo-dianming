# -*- coding: utf-8 -*-
"""午托点名 v1.5 —— 任务 C：科目档案界面 / 一师多科 / 签到选科 端到端自测

真机（无头 Edge + CDP）验证：
  1. 科目页签：列表 + 引用计数、搜索、只看启用、新增、编辑、启停、
     重复名拦截（界面层）、被引用科目删除保护（提示停用并阻止硬删）
  2. 老师档案：表单科目多选（含停用科目可见并标明）、行显示多科目（数学、奥数）、
     保存后 subjectIds 与显示一致
  3. 签到选科：科目下拉、老师任教科目优先（交集）、无配置回退全部启用科目、
     新建课程科目必选提示、唯一任教科目自动预选、编辑旧课程可保留无科目且不清历史文本
  4. 课程卡片、明细、统计（学生 / 老师 / 按科目 ID 归并 + 旧文本回退）显示科目名称
  5. 科目改名：历史课程快照不变，统计按 ID 归并不拆组
  6. 模式隔离、培训四项底部导航、档案三页签；午托点名页回归
  7. 390×844 无横向滚动、无乱码、无外部网络资源；刷新后科目、老师关联、
     课程科目与统计仍正确

用法（本机）：
  C:\\Users\\James\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\e2e_v15_subject_ui.py
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
HTTP_PORT = 8097
CDP_PORT = 9446
PROFILE = os.path.join(ROOT, "tests", "_e2e_v15_subject_profile")
SHOT_DIR = os.path.join(ROOT, "screenshots")          # v1.5 交付截图放根 screenshots/
REPORT = os.path.join(ROOT, "tests", "last-run-v15-subject-ui.json")


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


# 每个阶段加载页面前先执行的清理：注销 Service Worker + 清 CacheStorage + 清 HTTP 缓存
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


def launch():
    _close_stale_cdp()
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--remote-debugging-port=%d" % CDP_PORT,
         "--remote-allow-origins=*", "--user-data-dir=" + PROFILE,
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
# 第 1 段：API 造数（含旧文本课程）
# --------------------------------------------------------------------------
JS_SETUP = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  await T.ready();
  eq_guard: {
    if (X.appVersion !== "1.6.2") {
      checks.push({name:"版本号为 1.6.2", ok:false, detail:X.appVersion});
      return {checks, fatal:"版本不对"};
    }
    checks.push({name:"版本号为 1.6.2", ok:true, detail:X.appVersion});
  }

  /* 测试统一从午托模式开始，再切入培训模式 */
  if (X.state.mode !== "wudian") { X.switchMode("wudian"); await sleep(300); }

  /* 清空培训五张表（不碰午托），再造数 */
  await T.restoreSnapshot({teachers:[], students:[], courses:[], attendance:[], subjects:[]});

  const S1 = await T.createSubject({name:"数学", note:"培优"});
  const S2 = await T.createSubject({name:"奥数"});
  const S3 = await T.createSubject({name:"英语"});
  const S4 = await T.createSubject({name:"美术"});
  await T.setSubjectActive(S4.id, false);            /* 美术停用 */

  const T1 = await T.createTeacher({name:"赵老师"});                /* 暂无科目 */
  const T2 = await T.createTeacher({name:"单科王", subjectIds:[S1.id]});  /* 唯一科目 */
  const T3 = await T.createTeacher({name:"钱老师", subjectIds:[S1.id, S2.id]}); /* 两科 */

  const P1 = await T.createStudent({name:"张小测", gradeClass:"五年级1班"});
  const P2 = await T.createStudent({name:"李小测", gradeClass:"六年级2班"});

  /* 旧文本课程：科目档案里没有"冷门旧文本"，subjectId 应为空 */
  const CL = await T.createCourse({date:"2026-11-05", startTime:"08:00", endTime:"09:00",
    teacherId:T1.id, subject:"冷门旧文本", studentIds:[P1.id]});

  ok("造数完成：4 科目 3 老师 2 学生 1 旧文本课程",
     T.listSubjects().length === 4 && T.listTeachers().length === 3 &&
     T.listStudents().length === 2 && T.listCourses().length === 1);
  ok("旧文本课程 subjectId 为空", T.getCourse(CL.id).subjectId === "", T.getCourse(CL.id).subjectId);
  eq("旧文本课程文本保留", T.getCourse(CL.id).subject, "冷门旧文本");

  /* 切到培训模式并落在签到页，日期指到课程当天 */
  X.switchMode("training");
  await sleep(320);
  X.state.date = "2026-11-05";
  X.state.trTab = "signin";
  X.renderTraining();
  await sleep(300);

  return {checks, ids:{S1:S1.id, S2:S2.id, S3:S3.id, S4:S4.id,
                       T1:T1.id, T2:T2.id, T3:T3.id,
                       P1:P1.id, P2:P2.id, CL:CL.id}};
})()
"""

# --------------------------------------------------------------------------
# 第 2 段：科目档案 UI
# --------------------------------------------------------------------------
JS_SUBJECT_UI = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const view = () => document.querySelector("#view");
  const subRows = () => document.querySelectorAll('#view .drow[data-kind="subject"]');
  const openSubjects = async () => {
    X.state.trTab = "archives";
    X.state.trArchSeg = "subjects"; X.state.trSearch = ""; X.state.trSubActiveOnly = false;
    X.renderTraining(); await sleep(260);
  };
  const toastText = () => (document.querySelector("#toast") || {}).textContent || "";

  await openSubjects();
  ok("科目页有搜索框与新增按钮", !!document.querySelector("#trSearch") && !!document.querySelector("#trAdd"));
  eq("科目列表渲染 4 行", subRows().length, 4);
  ok("每行显示引用计数", view().textContent.indexOf("0 位老师 · 0 节课") >= 0 ||
     view().textContent.indexOf("位老师") >= 0, view().textContent.match(/\d 位老师 · \d 节课/g)?.join("|"));
  ok("停用科目带停用标记", view().textContent.indexOf("美术") >= 0 &&
     view().querySelector('.drow .pill.off') !== null);
  ok("空状态提示完整（有数据时不显示）", view().textContent.indexOf("还没有科目档案") < 0);

  /* 搜索 */
  const si = document.querySelector("#trSearch");
  si.value = "英语";
  si.dispatchEvent(new Event("input", {bubbles:true}));
  await sleep(300);
  eq("搜索过滤到 1 行", subRows().length, 1);
  /* 只看启用 */
  await openSubjects();
  document.querySelector("#trSubOnlyActive").click();
  await sleep(260);
  eq("只看启用 3 行（美术停用被滤掉）", subRows().length, 3);
  ok("只看启用按钮高亮", document.querySelector("#trSubOnlyActive").classList.contains("on"));
  document.querySelector("#trSubOnlyActive").click();
  await sleep(260);
  eq("取消过滤回到 4 行", subRows().length, 4);

  /* 新增科目 */
  document.querySelector("#trAdd").click();
  await sleep(240);
  ok("新增科目弹层有名称与备注", !!document.querySelector("#sheet #trSubName") &&
     !!document.querySelector("#sheet #trSubNote"));
  document.querySelector("#sheet #trSubName").value = "科学";
  document.querySelector("#sheet #trSubSave").click();
  await sleep(320);
  ok("新增科目成功并出现在列表", !!T.listSubjects({q:"科学"})[0]);
  eq("科目共 5 个", T.listSubjects().length, 5);
  eq("科目列表 5 行", subRows().length, 5);

  /* 重复名拦截（界面层） */
  document.querySelector("#trAdd").click();
  await sleep(240);
  document.querySelector("#sheet #trSubName").value = "  科学  ";
  document.querySelector("#sheet #trSubSave").click();
  await sleep(320);
  ok("重复名被拦截且弹层未关闭", !!document.querySelector("#sheet #trSubName"));
  ok("重复名提示文案正确", toastText().indexOf("已存在同名科目") >= 0, toastText());
  document.querySelector("#sheet [data-close]").click();
  await sleep(160);

  /* 编辑：启停下拉 + 保存 */
  const meishu = T.listSubjects({q:"美术"})[0];
  const meishuRow = Array.from(subRows()).find(r => r.textContent.indexOf("美术") >= 0);
  meishuRow.click();
  await sleep(240);
  ok("编辑弹层有启用状态下拉", !!document.querySelector("#sheet #trSubActive"));
  eq("美术当前为停用", document.querySelector("#sheet #trSubActive").value, "off");
  ok("编辑弹层显示引用计数说明", document.querySelector("#sheet").textContent.indexOf("位老师") >= 0);
  document.querySelector("#sheet #trSubActive").value = "on";
  document.querySelector("#sheet #trSubSave").click();
  await sleep(320);
  eq("美术改为启用", T.getSubject(meishu.id).active, true);
  await T.setSubjectActive(meishu.id, false);   /* 恢复停用，后续口径不变 */
  await sleep(120);

  /* 删除保护：数学被钱老师引用，删除须被阻止并提示停用 */
  const mathId = T.listSubjects({q:"数学"})[0].id;
  const mathRow = Array.from(subRows()).find(r => r.textContent.indexOf("数学") >= 0);
  mathRow.click();
  await sleep(240);
  document.querySelector("#sheet #trSubDelete").click();
  await sleep(240);
  ok("确认删除弹层出现", !!document.querySelector("#sheet #cfOk"));
  document.querySelector("#sheet #cfOk").click();
  await sleep(320);
  ok("被引用科目删除被阻止", !!T.getSubject(mathId));
  ok("删除保护提示改为停用", toastText().indexOf("不能删除") >= 0 && toastText().indexOf("停用") >= 0, toastText());

  /* 未被引用的科目可删（科学刚建，无引用） */
  const sciId = T.listSubjects({q:"科学"})[0].id;
  await T.removeSubject(sciId);
  eq("删除无引用科目后回到 4 个", T.listSubjects().length, 4);

  /* 科目页 390×844 无横向滚动（截图由外层脚本拍） */
  const noOv = document.documentElement.scrollWidth <= window.innerWidth + 1 &&
               document.body.scrollWidth <= window.innerWidth + 1;
  ok("科目页 390px 无横向滚动", noOv,
     document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth);
  ok("科目页无乱码", document.documentElement.textContent.indexOf("\uFFFD") < 0);

  return {checks};
})()
"""

# --------------------------------------------------------------------------
# 第 3 段：老师多科 + 签到选科 + 卡片 / 明细 / 统计
# --------------------------------------------------------------------------
JS_TEACHER_UI = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const view = () => document.querySelector("#view");
  const ids = window.__ids;
  const toastText = () => (document.querySelector("#toast") || {}).textContent || "";

  /* ---------- 老师档案：行显示多科目 ---------- */
  X.state.trTab = "archives";
  X.state.trArchSeg = "teachers"; X.state.trSearch = "";
  X.renderTraining(); await sleep(280);
  const zhaoRow = Array.from(view().querySelectorAll(".row")).find(r => r.textContent.indexOf("赵老师") >= 0);
  ok("赵老师无科目时显示未填科目", zhaoRow.textContent.indexOf("未填科目") >= 0, zhaoRow.textContent);

  /* 编辑赵老师：勾选 数学 + 奥数 + 美术（停用科目可见并标明） */
  zhaoRow.querySelector("[data-act=edit]").click();
  await sleep(260);
  ok("老师表单有科目多选列表", !!document.querySelector("#sheet #trSubPickList"));
  eq("多选列表含全部 4 个科目（含停用）",
     document.querySelectorAll("#sheet #trSubPickList .trpick").length, 4);
  const meishuPick = Array.from(document.querySelectorAll("#sheet #trSubPickList .trpick"))
    .find(r => r.textContent.indexOf("美术") >= 0);
  ok("停用科目在编辑时可见并标明", meishuPick.textContent.indexOf("已停用") >= 0);
  Array.from(document.querySelectorAll("#sheet #trSubPickList .trpick"))
    .filter(r => /数学|奥数|美术/.test(r.textContent))
    .forEach(r => r.click());
  document.querySelector("#sheet #trSave").click();
  await sleep(320);
  const zhao = T.getTeacher(ids.T1);
  eq("赵老师 subjectIds 3 个", zhao.subjectIds.length, 3);
  ok("subjectIds 含数学奥数美术",
     zhao.subjectIds.indexOf(ids.S1) >= 0 && zhao.subjectIds.indexOf(ids.S2) >= 0 &&
     zhao.subjectIds.indexOf(ids.S4) >= 0, JSON.stringify(zhao.subjectIds));
  eq("subject 文本与所选一致（按档案排序）", zhao.subject, "奥数、数学、美术");
  const zhaoRow2 = Array.from(view().querySelectorAll(".row")).find(r => r.textContent.indexOf("赵老师") >= 0);
  ok("老师行显示多个任教科目", zhaoRow2.textContent.indexOf("奥数、数学、美术") >= 0, zhaoRow2.textContent);

  /* 新增老师：多选 */
  document.querySelector("#trAdd").click();
  await sleep(260);
  document.querySelector("#sheet #trName").value = "周新师";
  Array.from(document.querySelectorAll("#sheet #trSubPickList .trpick"))
    .filter(r => /数学|英语/.test(r.textContent) && r.textContent.indexOf("美术") < 0)
    .forEach(r => r.click());
  document.querySelector("#sheet #trSave").click();
  await sleep(320);
  const zhou = T.listTeachers({q:"周新师"})[0];
  ok("新增老师挂两科", !!zhou && zhou.subjectIds.length === 2, JSON.stringify(zhou && zhou.subjectIds));
  const zhouRow = Array.from(view().querySelectorAll(".row")).find(r => r.textContent.indexOf("周新师") >= 0);
  ok("新增老师行显示“数学、英语”", zhouRow.textContent.indexOf("数学、英语") >= 0, zhouRow.textContent);

  /* ---------- 签到选科 ---------- */
  X.state.trTab = "signin"; X.state.trSearch = "";
  X.renderTraining(); await sleep(300);
  ok("签到页显示旧文本课程卡片", view().textContent.indexOf("冷门旧文本") >= 0);

  document.querySelector("#trNewCourse").click();
  await sleep(260);
  const subSel = document.querySelector("#sheet #trSubject");
  ok("科目改为下拉选择", subSel && subSel.tagName === "SELECT");
  eq("新课未选老师时回退全部启用科目", subSel.options.length, 4);  /* 空 + 数学/奥数/英语（美术停用被滤） */

  /* 美术停用不在回退列表：重算 */
  const optNames = () => Array.from(subSel.options).map(o => o.textContent);
  ok("回退列表只含启用科目", optNames().join("|").indexOf("美术") < 0, optNames().join("|"));

  /* 选钱老师（数学+奥数）：候选为交集 */
  const tSel = document.querySelector("#sheet #trTeacher");
  tSel.value = ids.T3;
  tSel.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(160);
  eq("钱老师候选 = 任教科目 2 个", subSel.options.length, 3);  /* 空 + 数学 + 奥数 */
  ok("候选不含科学等其它科目", optNames().join("|").indexOf("英语") < 0, optNames().join("|"));

  /* 选单科王：唯一科目自动预选 */
  tSel.value = ids.T2;
  tSel.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(160);
  eq("唯一任教科目自动预选", subSel.value, ids.S1);

  /* 选赵老师（数学、奥数、美术停用）：候选 3 个含停用标注 */
  tSel.value = ids.T1;
  tSel.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(160);
  eq("赵老师候选含停用美术共 3+1", subSel.options.length, 4);
  ok("停用候选标明已停用", optNames().join("|").indexOf("已停用") >= 0, optNames().join("|"));

  /* 保存课程挂到钱老师名下，赵老师统计就只剩旧文本课程一节 */
  tSel.value = ids.T3;
  tSel.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(160);

  /* 科目必选：不选科目保存被阻止（v1.6.2 时间下拉：设开始后派发 change 生成结束候选） */
  document.querySelector("#sheet #trDate").value = "2026-11-05";
  const startSelSub = document.querySelector("#sheet #trStart");
  startSelSub.value = "10:00";
  startSelSub.dispatchEvent(new Event("change", {bubbles:true}));
  await sleep(80);
  document.querySelector("#sheet #trEnd").value = "11:00";
  const pick1 = document.querySelector('#sheet .trpick[data-sid="' + ids.P1 + '"]');
  pick1.click(); await sleep(60);
  pick1.querySelector('[data-st="present"]').click(); await sleep(60);
  const beforeN = T.listCourses({date:"2026-11-05"}).length;
  document.querySelector("#sheet #trCourseSave").click();
  await sleep(320);
  ok("未选科目显示必选提示",
     document.querySelector("#sheet #trErr").style.display === "block" &&
     document.querySelector("#sheet #trErr").textContent.indexOf("请选择科目") >= 0,
     document.querySelector("#sheet #trErr").textContent);
  eq("未选科目不新增课程", T.listCourses({date:"2026-11-05"}).length, beforeN);

  /* 选科目（奥数）后保存成功，写 subjectId + 名称快照 */
  subSel.value = ids.S2;
  document.querySelector("#sheet #trCourseSave").click();
  await sleep(380);
  const newC = T.listCourses({date:"2026-11-05"}).filter(c => c.subjectId === ids.S2)[0];
  ok("选科目后保存成功", !!newC);
  eq("课程写 subjectId", newC && newC.subjectId, ids.S2);
  eq("课程写名称快照", newC && newC.subjectName, "奥数");
  eq("旧 subject 文本同步", newC && newC.subject, "奥数");

  /* 卡片显示科目名 */
  X.state.trTab = "signin";
  X.renderTraining(); await sleep(320);
  ok("当日卡片显示科目名“奥数”", view().textContent.indexOf("奥数") >= 0);

  /* 明细弹层显示科目名 */
  const card = Array.from(view().querySelectorAll(".trcourse"))
    .find(b => b.textContent.indexOf("奥数") >= 0);
  card.click();
  await sleep(280);
  ok("明细弹层显示科目与人数", document.querySelector("#sheet").textContent.indexOf("奥数") >= 0 &&
     document.querySelector("#sheet").textContent.indexOf("1 人") >= 0);
  document.querySelector("#sheet [data-close]").click();
  await sleep(180);

  /* 编辑旧课程：保持未填科目，历史文本不被清掉 */
  const legacyCard = Array.from(view().querySelectorAll(".trcourse"))
    .find(b => b.textContent.indexOf("冷门旧文本") >= 0);
  ok("旧文本课程卡片仍显示旧文本", !!legacyCard);
  legacyCard.click();
  await sleep(280);
  document.querySelector("#sheet #trEditCourse").click();
  await sleep(300);
  const legacySel = document.querySelector("#sheet #trSubject");
  eq("旧课程编辑保持未填科目", legacySel.value, "");
  ok("未填选项文案正确", legacySel.options[0].textContent.indexOf("保持未填科目") >= 0,
     legacySel.options[0].textContent);
  document.querySelector("#sheet #trCourseSave").click();
  await sleep(380);
  const legacyAfter = T.getCourse(ids.CL);
  eq("旧课程仍无 subjectId", legacyAfter.subjectId || "", "");
  eq("旧课程历史文本保留", legacyAfter.subject, "冷门旧文本");

  /* ---------- 统计（按科目 ID 归并 + 旧文本回退） ---------- */
  const stu = X.trStudentStats(ids.P1, {from:"2026-11-01", to:"2026-11-30"});
  eq("学生统计上课 2 次", stu.total, 2);
  eq("按科目归并 2 组（ID + 旧文本）", Object.keys(stu.bySubject).length, 2);
  const idKeys = Object.keys(stu.bySubject).filter(k => k.indexOf("id:") === 0);
  const txtKeys = Object.keys(stu.bySubject).filter(k => k.indexOf("txt:") === 0);
  eq("ID 组 1 个", idKeys.length, 1);
  eq("文本组 1 个", txtKeys.length, 1);
  ok("文本组保留旧文本名称", stu.bySubjectNames[txtKeys[0]] === "冷门旧文本", JSON.stringify(stu.bySubjectNames));

  /* 科目改名：历史课程快照不变，统计按 ID 归并组名用现名 */
  await T.updateSubject(ids.S2, {name:"奥数培优"});
  eq("改名后历史课程快照不变", T.getCourse(newC.id).subjectName, "奥数");
  const stu2 = X.trStudentStats(ids.P1, {from:"2026-11-01", to:"2026-11-30"});
  ok("统计仍按 ID 归并为一组", Object.keys(stu2.bySubject).filter(k => k.indexOf("id:") === 0).length === 1);
  ok("统计组名用科目现名", stu2.bySubjectNames[idKeys[0]] === "奥数培优", JSON.stringify(stu2.bySubjectNames));
  await T.updateSubject(ids.S2, {name:"奥数"});

  /* 老师统计：赵老师 1 节（旧文本课程） */
  const tt = X.trTeacherStats(ids.T1, {from:"2026-11-01", to:"2026-11-30"});
  eq("老师统计 1 节", tt.total, 1);
  eq("老师统计按文本归并 1 组", Object.keys(tt.bySubject).length, 1);
  ok("老师统计组名保留旧文本", Object.values(tt.bySubjectNames)[0] === "冷门旧文本",
     JSON.stringify(tt.bySubjectNames));

  /* 统计界面渲染 */
  X.state.trTab = "stats"; X.state.trStatsMode = "student"; X.state.trStatsStudentId = ids.P1;
  X.renderTraining(); await sleep(320);
  ok("学生统计界面有按科目分组", view().textContent.indexOf("按科目") >= 0 &&
     view().textContent.indexOf("冷门旧文本") >= 0);

  /* CSV 文本：科目列用快照 / 旧文本，不出现 undefined */
  const csvRows = [["日期","开始","结束","老师","科目","状态"],
                   ["2026-11-05","10:00","11:00","赵老师","奥数","出勤"]];
  ok("CSV 工具正常", X.trCsvText(csvRows).indexOf("奥数") >= 0);

  /* 390×844 无横向滚动（签到页 / 统计页） */
  const noOv = document.documentElement.scrollWidth <= window.innerWidth + 1 &&
               document.body.scrollWidth <= window.innerWidth + 1;
  ok("统计页 390px 无横向滚动", noOv,
     document.documentElement.scrollWidth + "/" + document.body.scrollWidth + "/" + window.innerWidth);

  return {checks, newCourseId: newC && newC.id};
})()
"""

# --------------------------------------------------------------------------
# 第 4 段：刷新后持久化 + 回归
# --------------------------------------------------------------------------
JS_AFTER_RELOAD = r"""
(async () => {
  const T = window.Training, X = window.__test;
  const checks = [];
  const ok = (n, c, d) => checks.push({name:n, ok:!!c, detail:d==null? String(c) : String(d)});
  const eq = (n, a, b) => checks.push({name:n, ok:a===b, detail:`期望 ${b}，实际 ${a}`});
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const ids = window.__ids;

  await T.ready();
  eq("刷新后版本仍 1.6.2", X.appVersion, "1.6.2");
  eq("刷新后科目仍 5 个（冷门旧文本文本被归档）", X.trCounts().subjects, 5);
  const zhao = T.getTeacher(ids.T1);
  eq("刷新后赵老师 subjectIds 仍在（合成文本不再归档）", zhao.subjectIds.length, 3);
  const legacySub = T.listSubjects({q:"冷门旧文本"})[0];
  ok("刷新后旧文本课程挂上归档科目", !!legacySub && T.getCourse(ids.CL).subjectId === legacySub.id,
     T.getCourse(ids.CL).subjectId);
  eq("刷新后旧课程文本保留", T.getCourse(ids.CL).subject, "冷门旧文本");
  const stu = X.trStudentStats(ids.P1, {from:"2026-11-01", to:"2026-11-30"});
  eq("刷新后统计仍 2 组", Object.keys(stu.bySubject).length, 2);

  /* 培训底部导航与档案三页签回归 */
  if (X.state.mode !== "training") { X.switchMode("training"); await sleep(320); }
  ok("培训底部恰好四项导航", document.querySelectorAll("#tabBar .tab").length === 4);
  X.state.trTab = "archives"; X.state.trArchSeg = "students"; X.state.trSearch = "";
  X.renderTraining(); await sleep(280);
  ok("档案内部三页签存在", !!document.querySelector("#trArchSeg") &&
     document.querySelectorAll("#trArchSeg button").length === 3);

  /* 午托回归：切换回午托模式，点名页正常且无培训数据 */
  X.switchMode("wudian");
  await sleep(320);
  X.switchTab("roster"); await sleep(250);
  X.switchTab("roll"); await sleep(350);
  ok("午托点名页正常渲染（空名单提示）", !!document.querySelector("#grid") ||
     document.querySelector("#view").textContent.indexOf("还没有学生") >= 0,
     document.querySelector("#view").textContent.slice(0, 60));
  ok("培训学生不出现在午托点名页", document.querySelector("#view").textContent.indexOf("张小测") < 0);
  eq("午托学生 0 名（隔离）", X.state.students.length, 0);

  /* 全局：无乱码、无外部资源 */
  ok("页面无乱码字符", document.documentElement.textContent.indexOf("\uFFFD") < 0);
  const ext = Array.from(document.querySelectorAll("[src],[href]")).filter(el => {
    const v = el.getAttribute("src") || el.getAttribute("href") || "";
    return /^https?:\/\//i.test(v);
  });
  eq("页面无外部网络资源", ext.length, 0);

  return {checks};
})()
"""


def main():
    srv = serve()
    page = None
    results = {"checks": [], "fatal": None, "screenshots": []}
    try:
        page = launch()
        page.send("Page.navigate",
                  {"url": "http://127.0.0.1:%d/tests/__no_such_subject_ui__" % HTTP_PORT})
        time.sleep(0.6)
        page.js(JS_CLEAN_ENV)
        page.goto("http://127.0.0.1:%d/index.html" % HTTP_PORT)

        print("[1/5] API 造数：科目 / 一师多科 / 旧文本课程")
        setup = page.js(JS_SETUP)
        results["checks"].extend(setup["checks"])
        ids = setup.get("ids", {})
        page.js("window.__ids = %s" % json.dumps(ids))

        print("[2/5] 390×844 视口：科目档案 UI")
        page.send("Emulation.setDeviceMetricsOverride", {
            "width": 390, "height": 844, "deviceScaleFactor": 1,
            "mobile": True, "screenWidth": 390, "screenHeight": 844
        })
        r2 = page.js(JS_SUBJECT_UI)
        results["checks"].extend(r2["checks"])
        page.js("X = window.__test; X.state.trArchSeg='subjects'; X.state.trSearch=''; "
                "X.state.trSubActiveOnly=false; X.renderTraining(); true;")
        time.sleep(0.5)
        results["screenshots"].append(page.shot("v15-training-subjects"))

        print("[3/5] 老师多科 / 签到选科 / 卡片明细统计")
        r3 = page.js(JS_TEACHER_UI)
        results["checks"].extend(r3["checks"])

        page.js("X = window.__test; try{document.querySelectorAll('#sheet [data-close]').forEach(function(b){b.click()})}catch(e){}; "
                "X.state.trTab='signin'; X.state.trSearch=''; X.renderTraining(); true;")
        time.sleep(0.6)
        results["screenshots"].append(page.shot("v15-training-signin"))
        page.js("X = window.__test; X.state.trTab='stats'; X.state.trStatsMode='student'; "
                "X.state.trStatsStudentId=%s; X.renderTraining(); true;" % json.dumps(ids.get("P1")))
        time.sleep(0.5)
        results["screenshots"].append(page.shot("v15-training-stats"))

        print("[4/5] 刷新验证持久化 + 午托回归")
        page.send("Page.reload")
        time.sleep(1.0)
        page.wait_ready()
        page.js("window.__ids = %s" % json.dumps(ids))
        r4 = page.js(JS_AFTER_RELOAD)
        results["checks"].extend(r4["checks"])
        page.js("X = window.__test; X.switchMode('wudian'); true;")
        time.sleep(0.5)
        page.js("X = window.__test; X.state.tab='roll'; X.renderTabBar(); X.switchTab('roll'); true;")
        time.sleep(0.5)
        results["screenshots"].append(page.shot("v15-wudian-roll"))

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
