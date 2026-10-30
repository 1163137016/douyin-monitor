#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音监控 · 本地缓存服务（浏览器只连 localhost，不直连 GitHub）

把云端画廊(GitHub Pages + GitHub Releases)镜像到本机：
  - 画廊页面在 localhost 本地起服务，秒开、可搜索、可账号切换；
  - 媒体(图片/视频)第一次观看时，由本服务从云端拉取并存入本地硬盘，
    之后直接从本地秒开、可离线观看；
  - 定期自动清理：超 MAX_AGE_DAYS 天未访问 或 总占用超 MAX_DISK_GB，自动删；
  - 云端始终是数据源，本地只缓存，不回传任何东西。

运行：
    python local_cache_server.py
然后浏览器打开 http://localhost:8000

可选环境变量：
    CATALOG_URL   云端 gallery.json 地址（默认项目 Pages）
    CACHE_DIR     本地缓存目录（默认 ./dm_cache）
    MAX_DISK_GB   缓存上限 GB（默认 5）
    MAX_AGE_DAYS  未访问天数上限（默认 30）
    PORT          监听端口（默认 8000）
"""
import base64
import html
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG_URL = os.environ.get(
    "CATALOG_URL",
    "https://1163137016.github.io/douyin-monitor/gallery.json",
)
CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(HERE / "dm_cache")))
MAX_DISK_BYTES = int(float(os.environ.get("MAX_DISK_GB", "5")) * 1024 ** 3)
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "30"))
PORT = int(os.environ.get("PORT", "8000"))

CACHE_DIR.mkdir(parents=True, exist_ok=True)
CATALOG_LOCAL = CACHE_DIR / "catalog.json"
_lock = threading.Lock()

# 后台预取(prefetch)进度
_prefetch = {"running": False, "total": 0, "done": 0, "errors": 0}


# ----------------------------- 工具 -----------------------------
def url_hash(u: str) -> str:
    return base64.urlsafe_b64encode(u.encode("utf-8")).decode("ascii")[:140]


def cache_path_for(u: str) -> Path:
    ext = ""
    m = re.search(r"\.([A-Za-z0-9]+)(\?|$)", urllib.parse.urlparse(u).path)
    if m:
        ext = m.group(1).lower()
    return CACHE_DIR / (url_hash(u) + ("." + ext if ext else ""))


def cloud_get(url: str, timeout: int = 240):
    req = urllib.request.Request(url, headers={"User-Agent": "dm-local-cache/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)


# ----------------------------- 目录(作品清单) -----------------------------
def load_catalog(force: bool = False):
    if CATALOG_LOCAL.exists() and not force:
        try:
            return json.loads(CATALOG_LOCAL.read_text("utf-8"))
        except Exception:
            pass
    try:
        with cloud_get(CATALOG_URL, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        CATALOG_LOCAL.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        return data
    except Exception as e:  # 云端不可达，退而用本地旧目录
        if CATALOG_LOCAL.exists():
            try:
                return json.loads(CATALOG_LOCAL.read_text("utf-8"))
            except Exception:
                pass
        raise RuntimeError(f"无法获取目录（云端不可达且无本地缓存）：{e}")


# ----------------------------- 缓存清理 -----------------------------
def clean_cache():
    now = time.time()
    age_limit = MAX_AGE_DAYS * 86400
    files = []
    total = 0
    for p in CACHE_DIR.iterdir():
        if p.name == "catalog.json" or p.suffix == ".part":
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((p, st.st_mtime, st.st_size))
        total += st.st_size
    # 1) 超期删除
    for p, mtime, size in files:
        if now - mtime > age_limit:
            try:
                p.unlink()
                total -= size
            except OSError:
                pass
    # 2) 超额删除（最久未访问优先）
    files2 = [(p, mtime, size) for (p, mtime, size) in files if p.exists()]
    files2.sort(key=lambda x: x[1])
    for p, mtime, size in files2:
        if total <= MAX_DISK_BYTES:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass
    # 3) 清理超过 1 小时的残留 .part
    for p in CACHE_DIR.glob("*.part"):
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass
    return total


def _clean_loop():
    while True:
        try:
            clean_cache()
        except Exception:
            pass
        time.sleep(600)  # 每 10 分钟


# ----------------------------- 后台预取 -----------------------------
def prefetch_all():
    with _lock:
        if _prefetch["running"]:
            return
        _prefetch.update(running=True, total=0, done=0, errors=0)
    try:
        cat = load_catalog()
        urls = []
        for acct in cat.get("accounts", []):
            for w in acct.get("works", []):
                for it in w.get("items", []):
                    u = it.get("url")
                    if u:
                        urls.append(u)
        with _lock:
            _prefetch["total"] = len(urls)
        for u in urls:
            cpath = cache_path_for(u)
            if cpath.exists() and cpath.stat().st_size > 0:
                with _lock:
                    _prefetch["done"] += 1
                continue
            try:
                part = cpath.with_suffix(cpath.suffix + ".part")
                with cloud_get(u, timeout=240) as r, open(part, "wb") as f:
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                if part.stat().st_size > 0:
                    os.replace(part, cpath)
                else:
                    part.unlink()
                with _lock:
                    _prefetch["done"] += 1
            except Exception:
                with _lock:
                    _prefetch["errors"] += 1
                try:
                    part.unlink()
                except Exception:
                    pass
    finally:
        with _lock:
            _prefetch["running"] = False


# ----------------------------- 画廊页面渲染 -----------------------------
def _esc(s):
    return html.escape(str(s or ""))


def _slug(s):
    s = (s or "").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]", "_", s)
    return s or "acct"


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音监控作品库（本地缓存 · 秒开）</title>
<style>
  :root{--bg:#f5f6f8;--panel:#fff;--ink:#222;--muted:#667;--brand:#2c3550;
        --accent:#3b6ef5;--accent-soft:#e8eefc;--line:#e6e8ec;--shadow:0 1px 4px rgba(0,0,0,.08)}
  *{box-sizing:border-box}
  body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;margin:0;background:var(--bg);color:var(--ink)}
  .topbar{position:sticky;top:0;z-index:20;display:flex;gap:12px;align-items:center;padding:10px 16px;
          background:rgba(255,255,255,.95);border-bottom:1px solid var(--line)}
  .brand{font-size:16px;font-weight:600;color:var(--brand);white-space:nowrap}
  .brand .sub{font-size:11px;font-weight:400;color:var(--muted);margin-left:6px}
  .search{flex:1;min-width:80px;max-width:360px;padding:8px 12px;border:1px solid var(--line);
          border-radius:20px;font-size:13px;outline:none}
  .search:focus{border-color:var(--accent)}
  .cache-btn{margin-left:auto;padding:6px 12px;border:1px solid var(--line);border-radius:16px;
             background:#fff;font-size:13px;cursor:pointer;color:var(--brand)}
  .cache-btn:hover{background:var(--accent-soft)}
  .layout{display:flex;align-items:flex-start;max-width:1200px;margin:0 auto}
  .sidebar{position:sticky;top:53px;align-self:flex-start;width:200px;flex:0 0 200px;max-height:calc(100vh - 53px);
           overflow:auto;padding:12px 10px;border-right:1px solid var(--line)}
  .sidebar .navtitle{font-size:12px;color:var(--muted);margin:4px 8px 8px}
  .chip{display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;text-align:left;
        padding:8px 10px;margin:3px 0;border:1px solid transparent;border-radius:8px;background:transparent;
        color:var(--ink);font-size:13px;cursor:pointer;line-height:1.3}
  .chip:hover{background:var(--accent-soft)}
  .chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .chip .n{font-size:11px;opacity:.8;flex:0 0 auto}
  .content{flex:1;min-width:0;padding:12px 18px 40px}
  .info{font-size:13px;color:var(--muted);margin:6px 2px 14px}
  h2.acct{font-size:15px;margin:22px 0 10px;padding:9px 12px;background:var(--accent-soft);
          border-radius:8px;color:var(--brand);display:flex;justify-content:space-between;align-items:baseline}
  h2.acct .cnt{font-weight:400;font-size:12px;color:var(--muted)}
  .work{background:var(--panel);border-radius:12px;padding:14px;margin:14px 0;box-shadow:var(--shadow)}
  h3{margin:0 0 8px;word-break:break-all;font-size:14px;color:#444}
  video,img{max-width:100%;border-radius:8px;margin:8px 0;display:block;background:#000}
  .live-badge{display:inline-block;background:#ff4d6d;color:#fff;font-size:12px;padding:2px 8px;
              border-radius:10px;margin:4px 0}
  pre{white-space:pre-wrap;word-break:break-all;background:#f2f3f5;padding:10px;border-radius:8px;
      font-size:13px;margin:8px 0 0}
  .empty{text-align:center;color:var(--muted);padding:60px 0}
  .cache-panel{position:fixed;right:14px;top:54px;z-index:50;width:280px;background:#fff;border:1px solid var(--line);
               border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.14);padding:14px;font-size:13px}
  .cache-panel[hidden]{display:none}
  .cp-row{display:flex;justify-content:space-between;padding:4px 0}
  .cp-actions{display:flex;gap:8px;margin:10px 0 6px;flex-wrap:wrap}
  .cp-actions button{flex:1;min-width:90px;padding:7px 0;border:1px solid var(--line);border-radius:8px;
                     background:#fff;cursor:pointer;font-size:12px}
  .cp-actions button:hover{background:var(--accent-soft)}
  .cp-note{color:var(--muted);font-size:11px;line-height:1.5;margin-top:4px}
  .bar{height:8px;background:#eef;border-radius:4px;overflow:hidden;margin:6px 0}
  .bar > i{display:block;height:100%;background:var(--accent);width:0}
  @media (max-width:720px){
    .layout{flex-direction:column}
    .sidebar{position:static;width:100%;flex:none;max-height:none;border-right:none;border-bottom:1px solid var(--line);
             display:flex;gap:6px;overflow:auto}
    .sidebar .navtitle{display:none}
    .chip{width:auto;white-space:nowrap;flex:0 0 auto}
    .content{padding:12px}
  }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">抖音监控作品库<span class="sub">本地缓存 · 秒开</span></div>
  <input id="q" class="search" placeholder="搜索标题 / 文案…" autocomplete="off">
  <button id="cacheBtn" class="cache-btn">本地缓存</button>
</div>
<div id="cachePanel" class="cache-panel" hidden>
  <div class="cp-row"><span>已缓存媒体</span><b id="cpCount">—</b></div>
  <div class="cp-row"><span>占用空间</span><b id="cpSize">—</b></div>
  <div class="cp-row"><span>目录生成时间</span><b id="cpGen">—</b></div>
  <div class="bar"><i id="cpBar"></i></div>
  <div class="cp-row"><span>预取进度</span><b id="cpPf">—</b></div>
  <div class="cp-actions">
    <button id="cpPfBtn">⬇ 缓存全部</button>
    <button id="cpRefresh">刷新目录</button>
  </div>
  <div class="cp-actions">
    <button id="cpClean">清理超期/超额</button>
    <button id="cpClear">清空缓存</button>
  </div>
  <div class="cp-note">本地只作缓存，云端为源；超 __AGE__ 天未看 或 超 __DISK__GB 自动删。点「缓存全部」可把所有媒体预下载到本机，之后离线也能看。</div>
</div>
<div class="layout">
<nav class="sidebar" id="nav">
  <div class="navtitle">账号切换</div>
  <!--CHIPS-->
</nav>
<main class="content">
  <div class="info" id="info"></div>
  <div id="sections"><!--SECTIONS--></div>
</main>
</div>
<script>
(function(){
  var chips=[].slice.call(document.querySelectorAll('.chip'));
  var sections=[].slice.call(document.querySelectorAll('.acct-section'));
  var q=document.getElementById('q'), info=document.getElementById('info'), active='all';
  function apply(){
    var term=q.value.trim().toLowerCase(), shownS=0, shownW=0;
    sections.forEach(function(s){
      var aid=s.getAttribute('data-acct'), matchAcct=(active==='all'||aid===active);
      var works=[].slice.call(s.querySelectorAll('.work')), cnt=0;
      works.forEach(function(w){
        var ok=matchAcct && (!term||w.textContent.toLowerCase().indexOf(term)>=0);
        w.style.display=ok?'':'none'; if(ok)cnt++;
      });
      var vis=(matchAcct && (cnt>0||!term));
      s.style.display=vis?'':'none';
      if(vis){shownS++; shownW+=cnt;}
    });
    chips.forEach(function(c){c.classList.toggle('active',c.getAttribute('data-acct')===active);});
    info.textContent='当前显示 '+shownW+' 个作品 · '+shownS+' 个账号'+(term?'（搜索：'+term+'）':'');
  }
  chips.forEach(function(c){c.addEventListener('click',function(){
    active=c.getAttribute('data-acct');
    if(active==='all'){history.replaceState(null,'',location.pathname+location.search);} else {location.hash=active;}
    apply();
  });});
  q.addEventListener('input',apply);
  var h=location.hash?location.hash.slice(1):'';
  if(h && document.querySelector('.chip[data-acct="'+CSS.escape(h)+'"]')) active=h;
  apply();
})();
(function(){
  var panel=document.getElementById('cachePanel'), btn=document.getElementById('cacheBtn');
  btn.addEventListener('click',function(){panel.hidden=!panel.hidden; if(!panel.hidden) refresh();});
  function fmt(b){ if(b==null) return '—'; var u=['B','KB','MB','GB','TB'],i=0;
    while(b>=1024&&i<u.length-1){b/=1024;i++;} return (i?b.toFixed(1):b.toFixed(0))+' '+u[i]; }
  function refresh(){ fetch('/api/status').then(r=>r.json()).then(function(d){
    document.getElementById('cpCount').textContent=d.count+' 个';
    document.getElementById('cpSize').textContent=fmt(d.bytes);
    document.getElementById('cpGen').textContent=d.generated||'—';
  }); pf(); }
  function pf(){ fetch('/api/prefetch_status').then(r=>r.json()).then(function(d){
    document.getElementById('cpPf').textContent=(d.total? (d.done+'/'+d.total+(d.running?' 进行中…':'')+'（失败'+d.errors+'）') : '无');
    var pct=d.total? Math.round(d.done/d.total*100):0;
    document.getElementById('cpBar').style.width=pct+'%';
  }); }
  document.getElementById('cpPfBtn').addEventListener('click',function(){
    if(!confirm('将把所有媒体从云端下载到本机缓存，可能耗时较久（取决于网络和总量）。继续？'))
      return;
    fetch('/api/prefetch',{method:'POST'}).then(function(){pf(); var t=setInterval(pf,2000);
      setTimeout(function(){clearInterval(t);}, 3600000);});
  });
  document.getElementById('cpRefresh').addEventListener('click',function(){
    fetch('/api/catalog_refresh',{method:'POST'}).then(r=>r.json()).then(function(d){
      if(d.ok) location.reload(); else alert('刷新失败：'+d.error);
    });
  });
  document.getElementById('cpClean').addEventListener('click',function(){
    fetch('/api/clean',{method:'POST'}).then(function(){refresh();});
  });
  document.getElementById('cpClear').addEventListener('click',function(){
    if(confirm('确定清空全部本地缓存？云端内容不受影响，之后观看会重新从云端拉取。'))
      fetch('/api/clear',{method:'POST'}).then(function(){refresh();});
  });
  setInterval(function(){ if(!panel.hidden) refresh(); }, 15000);
})();
</script>
</body>
</html>
"""


def render_gallery(cat: dict) -> str:
    accounts = cat.get("accounts", [])
    total = cat.get("total", sum(len(a.get("works", [])) for a in accounts))
    chips = ['  <button class="chip active" data-acct="all">全部 '
             f'<span class="n" id="n-all">{total}</span></button>']
    sections = []
    for a in accounts:
        aid = _slug(a.get("id") or a.get("nick") or "acct")
        works = a.get("works", [])
        chips.append(f'  <button class="chip" data-acct="{_esc(aid)}">'
                     f'{_esc(a.get("nick", ""))} <span class="n">{len(works)}</span></button>')
        inner = []
        for w in works:
            title = " · ".join(x for x in (w.get("pub", ""), w.get("desc", "")) if x)
            items = w.get("items", [])
            types = {it.get("type") for it in items}
            is_live = ("video" in types) and ("image" in types)
            inner.append(f'<div class="work"><h3>{_esc(title) or "未命名作品"}</h3>')
            if is_live:
                inner.append('<div class="live-badge">实况图 · 点击播放动态</div>')
            for it in items:
                u = it.get("url", "")
                eu = urllib.parse.quote(u, safe="")
                if it.get("type") == "video":
                    inner.append(
                        f'<video controls preload="none">'
                        f'<source src="/m?u={eu}" type="video/mp4"></source></video>')
                else:
                    inner.append(f'<img loading="lazy" src="/m?u={eu}" alt="">')
            if w.get("caption"):
                inner.append(f"<pre>{_esc(w['caption'])}</pre>")
            inner.append("</div>")
        sections.append(
            f'<section class="acct-section" data-acct="{_esc(aid)}" id="sec-{_esc(aid)}">\n'
            f'<h2 class="acct">{_esc(a.get("nick", ""))}'
            f'<span class="cnt">{len(works)} 条</span></h2>\n'
            f'{"".join(inner)}\n</section>')
    if not sections:
        sections.append('<div class="empty">暂无作品<br>下次云端运行抓取到新作品后会出现。</div>')
    return (PAGE
            .replace("<!--CHIPS-->", "\n".join(chips))
            .replace("<!--SECTIONS-->", "\n".join(sections))
            .replace("__AGE__", str(MAX_AGE_DAYS))
            .replace("__DISK__", str(os.environ.get("MAX_DISK_GB", "5"))))


ERROR_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>本地缓存服务</title></head><body style="font-family:sans-serif;padding:40px;color:#333">
<h2>暂时无法打开画廊</h2>
<p>本地服务已启动，但拉取云端目录失败（你的网络可能暂时连不上 GitHub）。</p>
<p>错误：<code>{err}</code></p>
<p>你可以：</p>
<ul>
  <li>稍后点右上角「本地缓存 → 刷新目录」重试；</li>
  <li>如果之前成功过，会沿用上一次的本地目录，刷新页面即可；</li>
  <li>确认网络能访问 <code>https://1163137016.github.io/douyin-monitor/gallery.json</code>。</li>
</ul>
</body></html>"""


# ----------------------------- HTTP Handler -----------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "dm-local-cache/1.0"

    def log_message(self, *args):  # 静默默认日志
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self.serve_gallery()
        if p == "/api/status":
            return self.serve_status()
        if p == "/api/prefetch_status":
            return self.serve_prefetch_status()
        if p == "/m":
            return self.serve_media(u)
        return self._send(404, "<h1>404</h1>")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p == "/api/catalog_refresh":
            try:
                load_catalog(force=True)
                return self._send(200, json.dumps({"ok": True}), ctype="application/json")
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}),
                                  ctype="application/json")
        if p == "/api/prefetch":
            threading.Thread(target=prefetch_all, daemon=True).start()
            return self._send(200, json.dumps({"ok": True}), ctype="application/json")
        if p == "/api/clean":
            sz = clean_cache()
            return self._send(200, json.dumps({"ok": True, "bytes": sz}),
                              ctype="application/json")
        if p == "/api/clear":
            for f in CACHE_DIR.glob("*"):
                if f.name == "catalog.json":
                    continue
                try:
                    f.unlink()
                except OSError:
                    pass
            return self._send(200, json.dumps({"ok": True}), ctype="application/json")
        return self._send(404, "<h1>404</h1>")

    def serve_gallery(self):
        try:
            reload_flag = bool(urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("reload"))
            cat = load_catalog(force=reload_flag)
        except Exception as e:
            return self._send(200, ERROR_PAGE.format(err=html.escape(str(e))))
        self._send(200, render_gallery(cat))

    def serve_status(self):
        count = 0
        total = 0
        for f in CACHE_DIR.iterdir():
            if f.name == "catalog.json" or f.suffix == ".part":
                continue
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                pass
        gen = ""
        if CATALOG_LOCAL.exists():
            try:
                gen = json.loads(CATALOG_LOCAL.read_text("utf-8")).get("generated", "")
            except Exception:
                pass
        self._send(200, json.dumps({"count": count, "bytes": total,
                                    "generated": gen,
                                    "max_disk_gb": MAX_DISK_BYTES / 1024 ** 3,
                                    "max_age_days": MAX_AGE_DAYS}),
                  ctype="application/json")

    def serve_prefetch_status(self):
        with _lock:
            d = dict(_prefetch)
        self._send(200, json.dumps(d), ctype="application/json")

    def serve_media(self, u):
        src = (urllib.parse.parse_qs(u.query).get("u") or [""])[0]
        if not src:
            return self._send(400, "missing u")
        cpath = cache_path_for(src)
        # 命中本地缓存
        if cpath.exists() and cpath.stat().st_size > 0:
            try:
                os.utime(cpath, None)  # 更新访问时间（清理用）
            except OSError:
                pass
            return self.serve_file(cpath, src)
        # 未命中：从云端拉，边存边传给客户端（先写 .part，成功再改名）
        try:
            part = cpath.with_suffix(cpath.suffix + ".part")
            with cloud_get(src, timeout=240) as r, open(part, "wb") as f:
                ctype = (r.headers.get("Content-Type") or
                         mimetypes.guess_type(src)[0] or "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except Exception:
                        pass  # 客户端已断开，仍继续把文件存完供下次用
                    f.write(chunk)
            if part.exists() and part.stat().st_size > 0:
                os.replace(part, cpath)
            elif part.exists():
                part.unlink()
            return
        except Exception as e:
            try:
                if part.exists() and part.stat().st_size == 0:
                    part.unlink()
            except Exception:
                pass
            return self._send(502, f"云端拉取失败：{html.escape(str(e))}")

    def serve_file(self, cpath: Path, src: str):
        ctype = mimetypes.guess_type(src)[0] or "application/octet-stream"
        size = cpath.stat().st_size
        rng = self.headers.get("Range")
        m = rng and re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(cpath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(cpath, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


def main():
    threading.Thread(target=_clean_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("抖音监控 · 本地缓存服务已启动")
    print(f"  浏览器打开:  http://localhost:{PORT}")
    print(f"  本地缓存目录: {CACHE_DIR}")
    print(f"  缓存上限: {MAX_DISK_BYTES/1024**3:.0f}GB · 未访问超 {MAX_AGE_DAYS} 天自动删")
    print("  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
