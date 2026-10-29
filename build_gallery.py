#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 GitHub Pages 画廊 site/index.html（可在线播放, 不依赖抖音）。

两种模式:
  1) 云端模式(推荐): 读取 state/<sec_uid>.json（每个账号一个文件）里的 works
     字段, 媒体直链指向远端存储(默认 GitHub Releases 附件直链)。网页本身只有
     几 KB, 作品数量不再受 GitHub Pages 容量限制; 画廊按账号分区展示。
  2) 本地模式: 扫描 downloads/ 并把媒体复制进 site/downloads/。
     受 Pages 1GB 产物上限约束, 仅适合作品量小的场景(未启用云端存储时自动使用)。

设计要点:
  - 全程只使用已归档的直链/本地文件, 不请求抖音, 不留下访问痕迹。
  - 每个作品一个卡片, 按发布时间倒序展示(最新在前)。
"""
import html
import json
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
  body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;
       max-width:880px;margin:0 auto;padding:16px;background:#f5f6f8;color:#222}
  h1{font-size:18px}
  .tip{font-size:13px;color:#666;margin:4px 0 16px}
  h2.acct{font-size:15px;margin:22px 0 10px;padding:9px 12px;background:#e8eefc;
          border-radius:8px;color:#2c3550;display:flex;
          justify-content:space-between;align-items:baseline}
  h2.acct .cnt{font-weight:400;font-size:12px;color:#667}
  .work{background:#fff;border-radius:12px;padding:14px;margin:14px 0;
        box-shadow:0 1px 4px rgba(0,0,0,.08)}
  h3{margin:0 0 8px;word-break:break-all;font-size:14px;color:#444}
  video,img{max-width:100%;border-radius:8px;margin:8px 0;display:block;background:#000}
  pre{white-space:pre-wrap;word-break:break-all;background:#f2f3f5;
      padding:10px;border-radius:8px;font-size:13px;margin:8px 0 0}
</style>
</head>
<body>
<h1>抖音监控作品库（云端，可在线播放，不依赖抖音）</h1>
"""


def enc(p: str) -> str:
    """对相对路径做 URL 百分号编码(保留 / 分隔符)。"""
    return urllib.parse.quote(p, safe="/")


def esc(s) -> str:
    return html.escape(str(s or ""))


def media_tag(kind: str, src: str) -> str:
    if kind == "video":
        return f'<video controls preload="metadata" src="{esc(src)}"></video>'
    return f'<img loading="lazy" src="{esc(src)}" alt="" />'


def collect_from_state() -> list:
    """云端模式: 读取 state/*.json（每个账号一个文件）里的 works 字段。

    返回按账号分组的结果 [(nickname, [作品...]), ...]；
    账号之间按各自最新作品时间倒序，组内作品也按时间倒序。
    """
    if not STATE_DIR.exists():
        return []
    groups = []
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
                or p.stem)
        groups.append((nick, items))
    groups.sort(key=lambda g: (g[1][0].get("ts") or 0) if g[1] else 0,
                reverse=True)
    return groups


def collect_from_local() -> list:
    """本地模式: 扫描 downloads/, 并把媒体复制进 site/downloads/。"""
    if not DOWNLOAD_DIR.exists():
        return []
    dest_root = SITE / "downloads"
    shutil.copytree(DOWNLOAD_DIR, dest_root)
    entries = []
    for caption_file in sorted(dest_root.rglob("文案.txt")):
        folder = caption_file.parent
        rel = folder.relative_to(dest_root).as_posix()
        items = []
        for ext in ("mp4", "webm", "mov", "jpg", "jpeg", "png", "gif", "webp"):
            for f in sorted(folder.glob(f"*.{ext}")):
                kind = ("video" if f.suffix.lower() in (".mp4", ".webm", ".mov")
                        else "image")
                items.append({"type": kind,
                              "url": f"downloads/{enc(rel)}/{enc(f.name)}"})
        entries.append({
            "nick": rel.split("/")[0] if "/" in rel else "",
            "desc": folder.name,
            "pub": "",
            "ts": 0,
            "_sort": rel,
            "items": items,
            "caption": caption_file.read_text("utf-8", errors="ignore"),
        })
    entries.sort(key=lambda x: x["_sort"], reverse=True)
    return entries


def build() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()

    groups = collect_from_state()
    mode = "云端直链"
    if not groups:
        mode = "本地文件"
        entries = collect_from_local()
        groups = [("", entries)] if entries else []

    total = sum(len(items) for _, items in groups)
    n_acct = sum(1 for nick, _ in groups if nick)
    blocks = []
    for nick, items in groups:
        # 账号分区标题（把不同抖音账号的作品分开）
        if nick:
            blocks.append(f'<h2 class="acct">{esc(nick)}'
                          f'<span class="cnt">{len(items)} 条</span></h2>')
        for e in items:
            title = " · ".join(x for x in (e.get("pub", ""),
                                           e.get("desc", "")) if x)
            parts = [f'<div class="work"><h3>{esc(title or "未命名作品")}</h3>']
            for it in e.get("items", []):
                parts.append(media_tag(it.get("type", "image"), it.get("url", "")))
            if e.get("caption"):
                parts.append(f"<pre>{esc(e['caption'])}</pre>")
            parts.append("</div>")
            blocks.append("\n".join(parts))

    if not blocks:
        blocks.append('<div class="work"><h3>暂无作品</h3>'
                      '<p class="tip">下次运行抓取到新作品后会出现在这里。</p></div>')

    html_doc = (PAGE_HEAD
                + f'<p class="tip">共 {total} 个作品 · {n_acct} 个账号 · '
                  f'存储模式: {esc(mode)}</p>\n'
                + "".join(blocks)
                + "</body>\n</html>\n")
    (SITE / "index.html").write_text(html_doc, "utf-8", errors="ignore")
    size_kb = (SITE / "index.html").stat().st_size // 1024
    print(f"gallery built: {total} 个作品 / {n_acct} 个账号 -> site/index.html "
          f"({size_kb} KB, 模式: {mode})")


if __name__ == "__main__":
    build()
