#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 GitHub Pages 画廊 site/index.html（可在线播放, 不依赖抖音）。

两种模式:
  1) 云端模式(推荐): 读取 state/<sec_uid>.json（每个账号一个文件）里的 works
     字段, 媒体直链指向远端存储(默认 GitHub Releases 附件直链)。网页本身只有
     几 KB, 作品数量不再受 GitHub Pages 容量限制; 画廊按账号分区展示。

  2) 本地模式: 扫描 downloads/ 并把媒体复制进 site/downloads/。
     受 Pages 1GB 产物上限约束, 仅适合作品量小的场景(未启用云端存储时自动使用)。

本版新增【账号切换】能力:
  - 左侧(或移动端顶部)固定账号导航, 点击仅显示该账号作品, 支持「全部」。
  - 顶部搜索框实时按标题/文案过滤。
  - 当前选中账号写入 URL 锚点 (location.hash), 可分享/收藏特定账号视图。
  - 全程只使用已归档的直链/本地文件, 不请求抖音, 不留下访问痕迹。
"""
import html
import json
import re
import shutil
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
DOWNLOAD_DIR = ROOT / "downloads"
SITE = ROOT / "site"
# 每个监控账号一个独立状态文件 state/<sec_uid>.json
STATE_DIR = ROOT / "state"

# ---- PWA 已停用 ----
# 页面不再注册 Service Worker，也不再输出 manifest。
# 但 sw.js 仍要保留一个「自注销」版本：浏览器导航时会检查 sw.js 是否更新，
# 旧 SW 拉到这个新版本后会清空全部 Cache Storage 并注销自己，让老访客自动脱钩。
# （若直接删掉 sw.js，旧 SW 会因 404 拿不到更新而长期赖着不走，页面反而更卡。）
SW_JS = r"""// 抖音监控画廊 · 离线缓存已停用（本文件仅用于让旧版 SW 自行注销）
self.addEventListener('install', function(){ self.skipWaiting(); });
self.addEventListener('activate', function(e){
  e.waitUntil((async function(){
    try{
      var keys = await caches.keys();
      await Promise.all(keys.map(function(k){ return caches.delete(k); }));
    }catch(err){}
    try{ await self.registration.unregister(); }catch(err){}
    try{
      var cs = await self.clients.matchAll({type:'window'});
      cs.forEach(function(c){ try{ c.navigate(c.url); }catch(err){} });
    }catch(err){}
  })());
});
// 注销生效前若还有请求进来：直接放行，不拦截、不缓存。
self.addEventListener('fetch', function(){});
"""

PAGE_HEAD = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音监控作品库</title>
<meta name="theme-color" content="#ffffff">
<style>
  :root{--bg:#f5f6f8;--panel:#fff;--ink:#222;--muted:#667;--brand:#2c3550;
        --accent:#3b6ef5;--accent-soft:#e8eefc;--line:#e6e8ec;--shadow:0 1px 4px rgba(0,0,0,.08)}
  *{box-sizing:border-box}
  body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;
       margin:0;background:var(--bg);color:var(--ink)}
  .topbar{position:sticky;top:0;z-index:20;display:flex;gap:12px;align-items:center;
          padding:10px 16px;background:rgba(255,255,255,.92);
          backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
  .brand{font-size:16px;font-weight:600;color:var(--brand);white-space:nowrap}
  .brand .sub{font-size:11px;font-weight:400;color:var(--muted);margin-left:6px}
  .search{flex:1;min-width:80px;max-width:360px;padding:8px 12px;border:1px solid var(--line);
          border-radius:20px;font-size:13px;outline:none;background:#fff}
  .search:focus{border-color:var(--accent)}
  .layout{display:flex;align-items:flex-start;max-width:1200px;margin:0 auto}
  .sidebar{position:sticky;top:53px;align-self:flex-start;width:200px;flex:0 0 200px;
           max-height:calc(100vh - 53px);overflow:auto;padding:12px 10px;
           border-right:1px solid var(--line)}
  .sidebar .navtitle{font-size:12px;color:var(--muted);margin:4px 8px 8px}
  .chip{display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;
        text-align:left;padding:8px 10px;margin:3px 0;border:1px solid transparent;
        border-radius:8px;background:transparent;color:var(--ink);font-size:13px;
        cursor:pointer;line-height:1.3}
  .chip:hover{background:var(--accent-soft)}
  .chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .chip .n{font-size:11px;opacity:.8;flex:0 0 auto}
  .chip.active .n{opacity:.9}
  .content{flex:1;min-width:0;padding:12px 18px 40px}
  .info{font-size:13px;color:var(--muted);margin:6px 2px 14px}
  h2.acct{font-size:15px;margin:22px 0 10px;padding:9px 12px;background:var(--accent-soft);
          border-radius:8px;color:var(--brand);display:flex;
          justify-content:space-between;align-items:baseline}
  h2.acct .cnt{font-weight:400;font-size:12px;color:var(--muted)}
  .work{background:var(--panel);border-radius:12px;padding:14px;margin:14px 0;
        box-shadow:var(--shadow);
        /* 屏幕外的卡片跳过布局与绘制：366 条约 3600 个媒体标签，
           不做这个在手机上首屏会很卡。不支持的浏览器会自动忽略。 */
        content-visibility:auto;contain-intrinsic-size:auto 420px}
  h3{margin:0 0 8px;word-break:break-all;font-size:14px;color:#444}
  video,img{max-width:100%;border-radius:8px;margin:8px 0;display:block;background:#000}
  video{max-height:72vh}
  .live-badge{display:inline-block;background:#ff4d6d;color:#fff;font-size:12px;
    padding:2px 8px;border-radius:10px;margin:4px 0;}
  .vbar{font-size:13px;margin:-2px 0 10px}
  .vbar a{color:#4da3ff;text-decoration:none}
  .vbar{font-size:12px;margin:0 0 6px}
  .vbar a{color:var(--accent);text-decoration:none}
  .vbar a:hover{text-decoration:underline}
  pre{white-space:pre-wrap;word-break:break-all;background:#f2f3f5;
      padding:10px;border-radius:8px;font-size:13px;margin:8px 0 0}
  .empty{text-align:center;color:var(--muted);padding:60px 0}
  /* ===== 近期更新 Top15 + 视频公告栏（与全站同一套圆角/间距/品牌色） ===== */
  .topzone{margin:2px 0 18px}
  .hl,.board{background:var(--panel);border:1px solid var(--line);border-radius:14px;
             padding:13px 14px;margin:0 0 10px;box-shadow:var(--shadow)}
  .board{margin-bottom:0}
  .hl-head,.board-head{display:flex;align-items:center;gap:8px;font-size:13.5px;
             font-weight:600;color:var(--brand);margin-bottom:11px}
  .hl-head .sub,.board-head .sub{font-size:11px;font-weight:400;color:var(--muted)}
  .hl-scroll,.board-scroll{display:flex;gap:10px;overflow-x:auto;padding:2px 2px 6px;
             scrollbar-width:thin;scrollbar-color:#d3d8e2 transparent;
             -webkit-overflow-scrolling:touch}
  .hl-scroll::-webkit-scrollbar,.board-scroll::-webkit-scrollbar{height:6px}
  .hl-scroll::-webkit-scrollbar-thumb,.board-scroll::-webkit-scrollbar-thumb{
             background:#d3d8e2;border-radius:3px}
  .hl-scroll::-webkit-scrollbar-track,.board-scroll::-webkit-scrollbar-track{background:transparent}
  .hl-card{flex:0 0 auto;width:88px;text-align:center;cursor:pointer;border-radius:12px;
             padding:8px 3px 7px;position:relative;border:1px solid transparent;
             transition:background .15s,border-color .15s}
  .hl-card:hover{background:var(--accent-soft)}
  .hl-card.active{border-color:var(--accent);background:var(--accent-soft)}
  .hl-ava{width:50px;height:50px;border-radius:50%;margin:0 auto 7px;display:flex;
             align-items:center;justify-content:center;color:#fff;font-size:19px;
             font-weight:600;box-shadow:0 2px 6px rgba(44,53,80,.18)}
  .hl-name{font-size:12px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;
             white-space:nowrap}
  .hl-time{font-size:10.5px;color:var(--muted);margin-top:3px}
  .hl-rk{position:absolute;top:5px;left:7px;font-size:9.5px;font-weight:600;
             color:#fff;border-radius:7px;padding:0 5px;line-height:14px;
             background:var(--brand);opacity:.9}
  .hl-card[data-rk="1"] .hl-rk{background:#e0a03c;opacity:1}
  .hl-card[data-rk="2"] .hl-rk{background:#98a2b3;opacity:1}
  .hl-card[data-rk="3"] .hl-rk{background:#c2895c;opacity:1}
  .bd-card{flex:0 0 auto;width:158px;cursor:pointer;border-radius:12px;overflow:hidden;
             background:var(--panel);border:1px solid var(--line);
             transition:transform .15s,box-shadow .15s,border-color .15s}
  .bd-card:hover{transform:translateY(-2px);box-shadow:0 6px 16px rgba(0,0,0,.1);
             border-color:#d6dbe6}
  .bd-thumb{position:relative;aspect-ratio:16/10;background:#eef0f4;display:flex;
             align-items:center;justify-content:center;overflow:hidden}
  .bd-thumb img{width:100%;height:100%;object-fit:cover;margin:0;border-radius:0}
  .bd-ph{font-size:22px;color:#aab2c0}
  .bd-tag{position:absolute;left:7px;bottom:7px;font-size:10px;background:rgba(20,24,35,.62);
             color:#fff;border-radius:6px;padding:1px 6px}
  .bd-desc{font-size:12px;color:var(--ink);line-height:1.45;padding:8px 9px 0;
             display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
             overflow:hidden;min-height:35px}
  .bd-meta{font-size:10.5px;color:var(--muted);padding:5px 9px 9px;display:flex;
             justify-content:space-between;gap:6px}
  .bd-meta span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .work,h2.acct{scroll-margin-top:66px}
  /* content-visibility 的卡片在屏外只有估算高度, 定位会漂; 跳转前临时关掉 */
  .work.cv-on{content-visibility:visible}
  .work.flash{box-shadow:0 0 0 3px var(--accent),var(--shadow);transition:box-shadow .3s}
  @media (max-width:720px){
    .layout{flex-direction:column}
    .sidebar{position:sticky;top:52px;z-index:15;width:100%;flex:none;max-height:none;
             background:rgba(255,255,255,.95);backdrop-filter:blur(6px);
             border-right:none;border-bottom:1px solid var(--line);display:flex;
             gap:6px;overflow:auto;padding:8px 10px}
    .sidebar .navtitle{display:none}
    .chip{width:auto;white-space:nowrap;flex:0 0 auto}
    .content{padding:12px}
    .hl-card{width:80px}
    .hl-ava{width:46px;height:46px;font-size:17px}
    .bd-card{width:146px}
    h2.acct,.work{scroll-margin-top:104px}
  }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">抖音监控作品库<span class="sub">云端 · 在线观看</span></div>
  <input id="q" class="search" placeholder="搜索标题 / 文案…" autocomplete="off">
</div>
<div class="layout">
<nav class="sidebar" id="nav">
  <div class="navtitle">账号切换</div>
  <button class="chip active" data-acct="all">全部 <span class="n" id="n-all">0</span></button>
  <!--ACCOUNTS-->
</nav>
<main class="content">
  <div class="info" id="info"></div>
  <!--HIGHLIGHTS-->
  <div id="sections"><!--SECTIONS--></div>
</main>
</div>
<script>
(function(){
  var chips=[].slice.call(document.querySelectorAll('.chip'));
  var sections=[].slice.call(document.querySelectorAll('.acct-section'));
  var q=document.getElementById('q');
  var info=document.getElementById('info');
  var active='all';
  function apply(){
    var term=q.value.trim().toLowerCase();
    var shownS=0,shownW=0;
    sections.forEach(function(s){
      var aid=s.getAttribute('data-acct');
      var matchAcct=(active==='all'||aid===active);
      var works=[].slice.call(s.querySelectorAll('.work'));
      var cnt=0;
      works.forEach(function(w){
        var ok=matchAcct && (!term||w.textContent.toLowerCase().indexOf(term)>=0);
        w.style.display=ok?'':'none';
        if(ok)cnt++;
      });
      var vis=(matchAcct && (cnt>0||!term));
      s.style.display=vis?'':'none';
      if(vis){shownS++;shownW+=cnt;}
    });
    chips.forEach(function(c){c.classList.toggle('active',c.getAttribute('data-acct')===active);});
    info.textContent='当前显示 '+shownW+' 个作品 · '+shownS+' 个账号'
      +(term?'（搜索：'+term+'）':'');
    var hlShown=0;
    [].forEach.call(document.querySelectorAll('.hl-card'),function(c){
      var ok=(active==='all'||c.getAttribute('data-acct')===active)
             &&(!term||(c.getAttribute('data-name')||'').toLowerCase().indexOf(term)>=0);
      c.style.display=ok?'':'none';
      if(ok) hlShown++;
      c.classList.toggle('active',active!=='all'&&c.getAttribute('data-acct')===active);
    });
    var bdShown=0;
    [].forEach.call(document.querySelectorAll('.bd-card'),function(c){
      var ok=(active==='all'||c.getAttribute('data-acct')===active)
             &&(!term||(c.getAttribute('data-txt')||'').toLowerCase().indexOf(term)>=0);
      c.style.display=ok?'':'none';
      if(ok) bdShown++;
    });
    /* 小标题跟着当前视图走, 否则"前 15 位作者"在单账号视图下是自相矛盾的 */
    var hs=document.querySelector('.hl-head .sub');
    if(hs) hs.textContent=(active==='all')
      ? (hlShown+' 位作者 · 点击切换') : '已切到该作者 · 再点一次回到全部';
    var bs=document.querySelector('.board-head .sub');
    if(bs) bs.textContent=bdShown+' 条视频 · 点击定位';
    toggleZone('hl'); toggleZone('board');
  }
  /* 该模块一张卡片都不剩时整块收起，避免留下空壳 */
  function toggleZone(cls){
    var cards=[].slice.call(document.querySelectorAll('.'+cls+'-card'));
    var any=cards.some(function(c){return c.style.display!=='none';});
    var box=document.querySelector('.'+cls);
    if(box) box.style.display=any?'':'none';
  }
  chips.forEach(function(c){
    c.addEventListener('click',function(){
      active=c.getAttribute('data-acct');
      if(active==='all'){history.replaceState(null,'',location.pathname+location.search);}
      else{location.hash=active;}
      apply();
    });
  });
  q.addEventListener('input',apply);
  /* 近期更新 Top15：点击切到该作者, 再点一次回到全部 */
  [].forEach.call(document.querySelectorAll('.hl-card'),function(c){
    c.addEventListener('click',function(){
      var acct=c.getAttribute('data-acct');
      active=(active===acct)?'all':acct;
      q.value=''; apply();
      history.replaceState(null,'',location.pathname+location.search
        +(active==='all'?'':'#'+active));
      if(active==='all'){ window.scrollTo({top:0,behavior:'smooth'}); return; }
      var el=document.getElementById('sec-'+active);
      if(el) el.scrollIntoView({behavior:'smooth',block:'start'});
    });
  });
  /* 视频公告栏：点击定位到具体作品并高亮 */
  [].forEach.call(document.querySelectorAll('.bd-card'),function(c){
    c.addEventListener('click',function(){
      var w=document.getElementById(c.getAttribute('data-w'));
      if(!w) return;
      var sec=w.closest?w.closest('.acct-section'):null;
      if(sec){ active=sec.getAttribute('data-acct'); }
      q.value=''; apply();        /* 清搜索: 保证目标作品没被过滤掉 */
      w.classList.add('cv-on');   /* 关掉离屏高度估算, 否则滚动定位会漂 */
      w.scrollIntoView({behavior:'smooth',block:'start'});
      w.classList.add('flash');
      setTimeout(function(){w.classList.remove('flash');},1800);
    });
  });
  var h=location.hash?location.hash.slice(1):'';
  if(h && document.querySelector('.chip[data-acct="'+CSS.escape(h)+'"]')){active=h;}
  apply();
})();
</script>
<script>
/* PWA 已停用：自动注销历史 Service Worker 并清空离线缓存，
   避免旧缓存接管请求导致页面变慢 / 内容不更新。只跑一次，无副作用。 */
(function(){
  if(!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.getRegistrations().then(function(rs){
    if(!rs.length) return;
    rs.forEach(function(r){ r.unregister(); });
    console.info('[PWA] 已注销 ' + rs.length + ' 个 Service Worker');
  }).catch(function(){});
  if(window.caches && caches.keys){
    caches.keys().then(function(keys){
      if(!keys.length) return;
      keys.forEach(function(k){ caches.delete(k); });
      console.info('[PWA] 已清空 ' + keys.length + ' 个离线缓存');
    }).catch(function(){});
  }
})();
</script>
</body>
</html>
"""


def slug(s: str) -> str:
    """生成安全的 HTML id/锚点: 保留中文与字母数字, 其余变下划线。"""
    s = (s or "").strip()
    if not s:
        return "acct"
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]", "_", s)
    return s or "acct"


def enc(p: str) -> str:
    """对相对路径做 URL 百分号编码(保留 / 分隔符)。"""
    return urllib.parse.quote(p, safe="/")


def esc(s) -> str:
    return html.escape(str(s or ""))


def media_tag(kind: str, src: str) -> str:
    if kind == "video":
        # 显式声明 type="video/mp4" 帮助浏览器识别; 另附「新窗口打开/下载」链接,
        # 即便个别旧附件 MIME 仍为 octet-stream, 用户点开也能观看或下载。
        return (f'<video controls preload="none">'
                f'<source src="{esc(src)}" type="video/mp4"></source>'
                f'</video>\n'
                f'<div class="vbar"><a href="{esc(src)}" target="_blank" '
                f'rel="noopener">⬇ 在新窗口打开 / 下载</a></div>')
    return f'<img loading="lazy" decoding="async" src="{esc(src)}" alt="" />'


def _work_card(e: dict, wid: str = "") -> str:
    title = " · ".join(x for x in (e.get("pub", ""), e.get("desc", "")) if x)
    items = e.get("items", [])
    types = {it.get("type") for it in items}
    # Live 图 / 动态照片: 同一作品同时含 image(封面) 与 video(动态),
    # 视频自带封面帧, 过滤掉静态图避免重复显示, 并标注实况。
    is_live = ("video" in types) and ("image" in types)
    # Live 图: 动态视频 + 其它静态图都展示(不丢内容); 视频自带封面帧。
    render = items
    idattr = f' id="{esc(wid)}"' if wid else ""
    parts = [f'<div class="work"{idattr}>', f'<h3>{esc(title or "未命名作品")}</h3>']
    if is_live:
        parts.append('<div class="live-badge">实况图 · 点击播放动态</div>')
    for it in render:
        parts.append(media_tag(it.get("type", "image"), it.get("url", "")))
    if e.get("caption"):
        parts.append(f"<pre>{esc(e['caption'])}</pre>")
    parts.append("</div>")
    return "\n".join(parts)


def collect_from_state() -> list:
    """云端模式: 读取 state/*.json（每个账号一个独立文件）。

    返回按账号分组的结果 [{id,nick,items}], 组内作品按时间倒序,
    账号之间按各自最新作品时间倒序。
    """
    groups = []
    if STATE_DIR.exists():
        for p in sorted(STATE_DIR.glob("*.json")):
            try:
                st = json.loads(p.read_text("utf-8"))
            except Exception as e:
                print(f"state 解析失败 {p.name}: {e}")
                continue
            works = st.get("works") or {}
            if not works:
                continue
            items = sorted(works.values(),
                           key=lambda x: x.get("ts") or 0, reverse=True)
            nick = (st.get("nickname") or (items[0].get("nick") if items else "")
                    or p.stem[:12])
            groups.append({"id": slug(p.stem), "nick": nick, "items": items})
    groups.sort(key=lambda g: (g["items"][0].get("ts") or 0) if g["items"] else 0,
                reverse=True)
    return groups


def collect_from_local() -> list:
    """本地模式: 扫描 downloads/, 媒体复制进 site/downloads/, 按账号分组。"""
    if not DOWNLOAD_DIR.exists():
        return []
    dest_root = SITE / "downloads"
    if dest_root.exists():
        shutil.rmtree(dest_root)
    shutil.copytree(DOWNLOAD_DIR, dest_root)
    by_nick = {}
    for caption_file in sorted(dest_root.rglob("文案.txt")):
        folder = caption_file.parent
        rel = folder.relative_to(dest_root).as_posix()
        nick = rel.split("/")[0] if "/" in rel else "未分组"
        items = []
        for ext in ("mp4", "webm", "mov", "jpg", "jpeg", "png", "gif", "webp"):
            for f in sorted(folder.glob(f"*.{ext}")):
                kind = ("video" if f.suffix.lower() in (".mp4", ".webm", ".mov")
                        else "image")
                items.append({"type": kind,
                              "url": f"downloads/{enc(rel)}/{enc(f.name)}"})
        entry = {"nick": nick, "desc": folder.name, "pub": "", "ts": 0,
                 "_sort": rel,
                 "items": items,
                 "caption": caption_file.read_text("utf-8", errors="ignore")}
        by_nick.setdefault(nick, []).append(entry)
    groups = []
    for nick, entries in by_nick.items():
        entries.sort(key=lambda x: x["_sort"], reverse=True)
        groups.append({"id": slug(nick), "nick": nick, "items": entries})
    groups.sort(key=lambda g: g["nick"])
    return groups


# ---------------------------------------------------------------------------
# 近期更新 Top15 + 视频公告栏
# ---------------------------------------------------------------------------
def _ts_of(e: dict) -> float:
    """取作品时间戳：优先 state 里的 ts，其次解析 pub 字符串。"""
    t = e.get("ts")
    try:
        if t:
            return float(t)
    except Exception:
        pass
    pub = (e.get("pub") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(pub, fmt))
        except Exception:
            continue
    return 0.0


def _rel_time(ts: float) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 30:
        return f"{int(d // 86400)} 天前"
    return time.strftime("%m-%d", time.localtime(ts))


def _ava_style(name: str, rank: int = 0) -> str:
    """头像底色：统一收在品牌蓝紫色域内( hue 206~252 ), 不再用随机彩虹色。

    彩虹色头像和整站的蓝灰品牌色冲突, 视觉上最"跳"。这里只让色相在窄区间
    内浮动以区分不同作者, 饱和度/亮度保持一致, 排名越靠前略微提亮。
    """
    h = 0
    for ch in str(name or "?"):
        h = (h * 31 + ord(ch)) % 360
    hue = 206 + (h % 47)              # 206~252: 蓝 → 蓝紫
    lift = max(0, 4 - rank)           # 前 3 名提亮 4/3/2
    s, l1, l2 = 58, 56 + lift, 44 + lift
    return (f"linear-gradient(135deg,hsl({hue} {s}% {l1}%),"
            f"hsl({hue + 10} {s}% {l2}%))")


def _highlights_html(groups: list) -> str:
    if not groups:
        return ""

    # ---- 近期更新 Top 15：按各账号最新作品时间倒序 ----
    ranked = sorted(groups,
                    key=lambda g: _ts_of(g["items"][0]) if g["items"] else 0,
                    reverse=True)[:15]
    cards = []
    for i, g in enumerate(ranked, 1):
        latest = _ts_of(g["items"][0]) if g["items"] else 0
        when = _rel_time(latest) or f'{len(g["items"])} 条'
        rk = f'<span class="hl-rk">{i}</span>' if i <= 3 else ""
        rkattr = f' data-rk="{i}"' if i <= 3 else ""
        nick = g["nick"]
        cards.append(
            f'<div class="hl-card" data-acct="{esc(g["id"])}"'
            f' data-name="{esc(nick)}"{rkattr}>{rk}'
            f'<div class="hl-ava" style="background:{_ava_style(nick, i)}">'
            f'{esc(nick[:1])}</div>'
            f'<div class="hl-name">{esc(nick)}</div>'
            f'<div class="hl-time">{esc(when)}</div>'
            f'</div>')
    top_html = ('<div class="hl"><div class="hl-head">🔥 近期更新'
                f'<span class="sub">前 {len(ranked)} 位作者 · 点击切换</span></div>'
                '<div class="hl-scroll">' + "".join(cards) + '</div></div>')

    # ---- 视频公告栏：全局最新 12 条视频 ----
    vids = []
    for gi, g in enumerate(groups):
        for ei, e in enumerate(g["items"]):
            types = {it.get("type") for it in e.get("items", [])}
            if "video" not in types:
                continue
            vids.append((_ts_of(e), gi, ei, g, e))
    vids.sort(key=lambda x: x[0], reverse=True)
    vids = vids[:12]
    if not vids:
        return f'<div class="topzone">{top_html}</div>'
    bdc = []
    for ts, gi, ei, g, e in vids:
        cover = ""
        for it in e.get("items", []):
            if it.get("type") == "image":
                cover = it.get("url", "")
                break
        thumb = (f'<img loading="lazy" decoding="async" src="{esc(cover)}" alt="">'
                 if cover else '<div class="bd-ph">▶</div>')
        desc = (e.get("desc") or "未命名作品")[:42]
        # data-txt 供搜索联动：作者 + 文案一起参与匹配
        txt = f'{g["nick"]} {e.get("desc") or ""}'
        bdc.append(
            f'<div class="bd-card" data-w="w-{gi}-{ei}" '
            f'data-acct="{esc(g["id"])}" data-txt="{esc(txt)}">'
            f'<div class="bd-thumb">{thumb}<span class="bd-tag">视频</span></div>'
            f'<div class="bd-desc">{esc(desc)}</div>'
            f'<div class="bd-meta"><span>{esc(g["nick"])}</span>'
            f'<span>{esc(_rel_time(ts))}</span></div></div>')
    board_html = ('<div class="board"><div class="board-head">📢 视频公告栏'
                  f'<span class="sub">最近更新的 {len(bdc)} 条视频 · 点击定位</span></div>'
                  '<div class="board-scroll">' + "".join(bdc) + '</div></div>')
    return f'<div class="topzone">{top_html}{board_html}</div>'


def build() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()

    groups = collect_from_state()
    mode = "云端直链"
    if not groups:
        mode = "本地文件"
        groups = collect_from_local()

    total = sum(len(g["items"]) for g in groups)
    n_acct = len(groups)

    # 账号导航
    chips = [f'  <button class="chip" data-acct="{g["id"]}">'
             f'{esc(g["nick"])} <span class="n">{len(g["items"])}</span></button>'
             for g in groups]
    nav_html = "\n".join(chips)

    # 各账号分区
    sec_parts = []
    for gi, g in enumerate(groups):
        inner = "".join(_work_card(e, f"w-{gi}-{ei}")
                        for ei, e in enumerate(g["items"]))
        sec_parts.append(
            f'<section class="acct-section" data-acct="{esc(g["id"])}" '
            f'id="sec-{esc(g["id"])}">\n'
            f'<h2 class="acct">{esc(g["nick"])}'
            f'<span class="cnt">{len(g["items"])} 条</span></h2>\n'
            f'{inner}\n</section>')
    sections_html = "\n".join(sec_parts)

    if not sec_parts:
        sections_html = ('<div class="empty">暂无作品<br>'
                         '下次运行抓取到新作品后会出现在这里。</div>')

    doc = (PAGE_HEAD
           .replace("<!--ACCOUNTS-->", nav_html)
           .replace("<!--HIGHLIGHTS-->", _highlights_html(groups))
           .replace("<!--SECTIONS-->", sections_html)
           .replace('<span class="n" id="n-all">0</span>',
                    f'<span class="n" id="n-all">{total}</span>')
           .replace('<div class="info" id="info"></div>',
                    f'<div class="info" id="info">共 {total} 个作品 · '
                    f'{n_acct} 个账号 · 存储模式: {esc(mode)}</div>'))

    (SITE / "index.html").write_text(doc, "utf-8", errors="ignore")
    (SITE / "sw.js").write_text(SW_JS, "utf-8", errors="ignore")

    # PWA 已停用：不再输出 manifest.webmanifest 与图标；
    # 旧版本遗留的同名文件若还在站点目录里，一并删掉，避免浏览器继续读到。
    for stale in ("manifest.webmanifest", "icon-192.png", "icon-512.png",
                  "apple-touch-icon.png"):
        f = SITE / stale
        if f.exists():
            try:
                f.unlink()
                print(f"    已移除旧 PWA 文件: {stale}")
            except Exception as e:
                print(f"    !! 移除 {stale} 失败: {e}")

    # 导出结构化索引 gallery.json: 供「本地缓存服务」读取, 避免解析 HTML。
    # 本地服务用它构建本地画廊 + 代理/缓存云端媒体, 实现页面本地秒开。
    index = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "source": "github-releases",
        "accounts": [
            {
                "id": g["id"],
                "nick": g["nick"],
                "works": [
                    {
                        "pub": e.get("pub", ""),
                        "desc": e.get("desc", ""),
                        "caption": e.get("caption", ""),
                        "kind": e.get("kind", ""),
                        "items": [{"type": it.get("type", "image"),
                                   "url": it.get("url", "")}
                                  for it in e.get("items", [])],
                    }
                    for e in g["items"]
                ],
            }
            for g in groups
        ],
    }
    (SITE / "gallery.json").write_text(
        json.dumps(index, ensure_ascii=False), "utf-8", errors="ignore")

    size_kb = (SITE / "index.html").stat().st_size // 1024
    print(f"gallery built: {total} 个作品 / {n_acct} 个账号 -> site/index.html "
          f"({size_kb} KB, 模式: {mode})")


if __name__ == "__main__":
    build()
