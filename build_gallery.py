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
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
DOWNLOAD_DIR = ROOT / "downloads"
SITE = ROOT / "site"
# 每个监控账号一个独立状态文件 state/<sec_uid>.json
STATE_DIR = ROOT / "state"

PAGE_HEAD = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音监控作品库（云端）</title>
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
        box-shadow:var(--shadow)}
  h3{margin:0 0 8px;word-break:break-all;font-size:14px;color:#444}
  video,img{max-width:100%;border-radius:8px;margin:8px 0;display:block;background:#000}
  pre{white-space:pre-wrap;word-break:break-all;background:#f2f3f5;
      padding:10px;border-radius:8px;font-size:13px;margin:8px 0 0}
  .empty{text-align:center;color:var(--muted);padding:60px 0}
  @media (max-width:720px){
    .layout{flex-direction:column}
    .sidebar{position:static;width:100%;flex:none;max-height:none;border-right:none;
             border-bottom:1px solid var(--line);display:flex;gap:6px;overflow:auto}
    .sidebar .navtitle{display:none}
    .chip{width:auto;white-space:nowrap;flex:0 0 auto}
    .content{padding:12px}
  }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">抖音监控作品库<span class="sub">云端 · 可在线播放</span></div>
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
  var h=location.hash?location.hash.slice(1):'';
  if(h && document.querySelector('.chip[data-acct="'+CSS.escape(h)+'"]')){active=h;}
  apply();
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
        return f'<video controls preload="metadata" src="{esc(src)}"></video>'
    return f'<img loading="lazy" src="{esc(src)}" alt="" />'


def _work_card(e: dict) -> str:
    title = " · ".join(x for x in (e.get("pub", ""), e.get("desc", "")) if x)
    parts = [f'<div class="work"><h3>{esc(title or "未命名作品")}</h3>']
    for it in e.get("items", []):
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
    for g in groups:
        inner = "".join(_work_card(e) for e in g["items"])
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
           .replace("<!--SECTIONS-->", sections_html)
           .replace('<span class="n" id="n-all">0</span>',
                    f'<span class="n" id="n-all">{total}</span>')
           .replace('<div class="info" id="info"></div>',
                    f'<div class="info" id="info">共 {total} 个作品 · '
                    f'{n_acct} 个账号 · 存储模式: {esc(mode)}</div>'))

    (SITE / "index.html").write_text(doc, "utf-8", errors="ignore")
    size_kb = (SITE / "index.html").stat().st_size // 1024
    print(f"gallery built: {total} 个作品 / {n_acct} 个账号 -> site/index.html "
          f"({size_kb} KB, 模式: {mode})")


if __name__ == "__main__":
    build()
