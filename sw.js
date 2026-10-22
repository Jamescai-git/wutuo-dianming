/* =====================================================================
   午托点名 —— Service Worker（cache-first 预缓存外壳）
   升级方式：改动 index.html 等文件后，把 VERSION 改一个号再部署。
   ===================================================================== */

var VERSION = "wudian-v1.2.1";
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

/* 取用：cache-first；导航请求回落 index.html，保证断网刷新可用 */
self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;

  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   /* 外链一律不接管 */

  if (req.mode === "navigate") {
    event.respondWith(
      caches.match("./index.html").then(function (hit) {
        return hit || fetch(req).catch(function () { return caches.match("./"); });
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
