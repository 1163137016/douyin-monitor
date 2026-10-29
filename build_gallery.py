#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据 downloads/ 生成 GitHub Pages 画廊 site/index.html（可在线播放, 不依赖抖音）。

用法: python build_gallery.py
产出:
  ./site/index.html           画廊页面(内置 <video>/<img> 播放器)
  ./site/downloads/...        复制过来的媒体文件(由 Actions 部署为 Pages)

设计要点:
  - 全程只接触已下载到本地的文件, 不请求抖音, 不留下访问痕迹。
  - 媒体文件名可能含中文/空格, URL 统一做百分号编码, 浏览器可正确加载。
  - 每个作品一个文件夹 + 文案.txt, 页面按发布时间倒序展示(最新在前)。
"""
import shutil
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
DOWNLOAD_DIR = ROOT / "downloads"
SITE = ROOT / "site"


def enc(p: str) -> str:
    """对相对路径做 URL 百分号编码(保留 / 分隔符)。"""
    return urllib.parse.quote(p, safe="/")


def build() -> None:
    if not DOWNLOAD_DIR.exists():
        print("downloads/ 不存在, 跳过画廊生成")
        return

    # 清空旧 site, 重建
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()

    # 复制媒体到 site/downloads (部署后即为可访问的云端文件)
    dest_root = SITE / "downloads"
    shutil.copytree(DOWNLOAD_DIR, dest_root)

    # 收集每个作品(以 文案.txt 定位作品文件夹)
    items = []
    for caption_file in sorted(dest_root.rglob("文案.txt")):
        folder = caption_file.parent
        rel = folder.relative_to(dest_root).as_posix()
        media = []
        for ext in ("mp4", "webm", "jpg", "jpeg", "png", "gif", "webp"):
            for f in sorted(folder.glob(f"*.{ext}")):
                media.append(f.name)
        caption = caption_file.read_text("utf-8", errors="ignore")
        items.append((rel, media, caption))

    # 最新在前(文件夹名以发布时间开头, 倒序即可)
    items.sort(key=lambda x: x[0], reverse=True)

    blocks = []
    for rel, media, caption in items:
        parts = [f'<div class="work"><h3>{rel}</h3>']
        for m in media:
            src = f"downloads/{enc(rel)}/{enc(m)}"
            if m.lower().endswith((".mp4", ".webm")):
                parts.append(
                    f'<video controls preload="metadata" src="{src}"></video>'
                )
            else:
                parts.append(f'<img loading="lazy" src="{src}" alt="{m}" />')
        parts.append(f"<pre>{caption}</pre>")
        parts.append("</div>")
        blocks.append("\n".join(parts))

    html = f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音监控作品库（云端）</title>
<style>
  body{{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;
       max-width:880px;margin:0 auto;padding:16px;background:#f5f6f8;color:#222}}
  h1{{font-size:18px}}
  .work{{background:#fff;border-radius:12px;padding:14px;margin:14px 0;
        box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  h3{{margin:0 0 8px;word-break:break-all;font-size:14px;color:#444}}
  video,img{{max-width:100%;border-radius:8px;margin:8px 0;display:block;background:#000}}
  pre{{white-space:pre-wrap;word-break:break-all;background:#f2f3f5;
       padding:10px;border-radius:8px;font-size:13px;margin:8px 0 0}}
</style>
</head>
<body>
<h1>抖音监控作品库（云端，可在线播放，不依赖抖音）</h1>
{''.join(blocks)}
</body>
</html>
"""
    (SITE / "index.html").write_text(html, "utf-8", errors="ignore")
    print(f"gallery built: {len(items)} 个作品 -> site/index.html")


if __name__ == "__main__":
    build()
