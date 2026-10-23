/* =====================================================================
   午托点名 —— Service Worker
   取用策略（v1.6.1 起，v1.6.2 沿用）：
     · 导航请求（打开 / 刷新页面）走 network-first：在线先取最新 index.html
       并写回缓存，网络失败才回退缓存 —— 断网照样能打开，在线优先拿到新版。
     · 静态资源仍走 cache-first（图标、manifest 等变化少）。
   升级方式：改动 index.html 等文件后，把 VERSION 改一个号再部署。
   ===================================================================== */

var VERSION = "wudian-v1.6.2";
var CACHE = "wudian-shell-" + VERSION;

/* 外壳资源：断网也要能打开，所以 index.html 必须进预缓存 */
var SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icons/icon-180.png",
  "./icons/icon-192.png",
  "./icons/icon-512.png"
];

/* 安装：一次性把外壳塞进缓存 */
self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      /* 逐个 add，单个失败不拖垮整次安装 */
      return Promise.all(
        SHELL.map(function (url) {
          return cache.add(new Request(url, { cache: "reload" })).catch(function () {
            return null;
          });
        })
      );
    }).then(function () { return self.skipWaiting(); })
  );
});

/* 激活：清掉旧版本缓存 */
self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (k) {
          if (k.indexOf("wudian-shell-") === 0 && k !== CACHE) return caches.delete(k);
          return null;
        })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

/* 取用：导航 network-first（在线拿最新页面，失败回退缓存）；
   其它同源静态资源 cache-first */
self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;

  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   /* 外链一律不接管 */

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).then(function (res) {
        /* 在线：先给用户最新页面，同时把新壳写回缓存，供断网时使用 */
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) {
            return c.put("./index.html", copy).catch(function () { return null; });
          });
        }
        return res;
      }).catch(function () {
        /* 离线：回退到缓存里的壳，保证仍能打开、仍能点名 */
        return caches.match("./index.html").then(function (hit) {
          return hit || caches.match("./");
        }).then(function (hit) {
          return hit || new Response(
            "<!doctype html><meta charset=\"utf-8\"><title>离线</title>" +
            "<p style=\"font:16px/1.6 system-ui;padding:24px\">当前没有网络，且本机还没有缓存页面。请联网后再打开一次。</p>",
            { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
        });
      })
    );
    return;
  }

  event.respondWith(
    caches.match(req, { ignoreSearch: true }).then(function (hit) {
      if (hit) return hit;
      return fetch(req).then(function (res) {
        /* 同源静态资源顺手入缓存，下次断网也能用 */
        if (res && res.ok && res.type === "basic") {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); });
        }
        return res;
      }).catch(function () {
        return new Response("", { status: 504, statusText: "offline" });
      });
    })
  );
});

/* 允许页面主动查询版本（便于排查缓存问题） */
self.addEventListener("message", function (event) {
  if (event.data === "version" && event.source) {
    event.source.postMessage({ type: "version", version: VERSION });
  }
});
