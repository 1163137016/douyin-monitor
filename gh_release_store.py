#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Releases 存储后端（STORAGE_BACKEND=gh_release，默认）。

把作品媒体上传到**本仓库**的 GitHub Release 附件，返回永久公开直链，
画廊 <video>/<img> 可直接播放。GitHub 官方限制：
  - 单个附件 < 2 GiB
  - 单个 release 最多 1000 个附件
  - 但 release 总大小与下载带宽均无限制，且附件不计入仓库体积
因此附件数用满时会自动新建下一个 release（works-1、works-2 …）。

所需配置（均无需手动创建）：
  GITHUB_TOKEN       Actions 自动生成的令牌（需在 workflow 里显式注入 env）
  GITHUB_REPOSITORY  Actions 自动提供，形如 owner/repo
未配置时 enabled() 返回 False，调用方自动退回本地模式，不会报错。
"""
import os
import time
import urllib.parse
from pathlib import Path

import requests

API = "https://api.github.com"
UPLOAD_API = "https://uploads.github.com"

# 单个 release 的附件上限（GitHub 限制 1000，留安全余量）
MAX_ASSETS = 950
TAG_PREFIX = "works-"
# 单个附件大小上限 2GiB，留余量
MAX_FILE = int(1.9 * 1024 ** 3)

_TOKEN = ""
_REPO = ""

# 当前正在写入的 release
_cur_tag = ""
_cur_id = None
_cur_count = 0
_ready = False

# 附件名 -> 直链，用于上传前幂等复用（回灌时避免重复上传同名文件）
_asset_index = {}


def reload():
    """重新读取配置（本地 .env 场景用；Actions 已通过 env 注入）。"""
    global _TOKEN, _REPO, _ready, _cur_tag, _cur_id, _cur_count
    _TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
    _REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
    _ready = False
    _cur_tag, _cur_id, _cur_count = "", None, 0


def enabled() -> bool:
    """令牌与仓库名齐全才视为可用；否则退回本地模式。"""
    reload()
    return bool(_TOKEN and _REPO and "/" in _REPO)


def _headers():
    return {"Authorization": f"token {_TOKEN}",
            "Accept": "application/vnd.github+json"}


def _list_releases():
    """列出本仓库所有 release（翻页）。

    返回 [{tag_name, id, n_assets, assets:[(name, url)]}]；
    assets 用于幂等判断——回灌历史作品时同名附件若已存在，直接复用其直链。
    """
    out = []
    for page in range(1, 11):  # 最多 1000 个 release，足够
        try:
            r = requests.get(f"{API}/repos/{_REPO}/releases", headers=_headers(),
                             params={"per_page": 100, "page": page}, timeout=30)
            if r.status_code != 200:
                break
            batch = r.json()
        except Exception as e:
            print(f"    !! 列取 release 失败: {e}")
            break
        if not batch:
            break
        for rel in batch:
            assets = rel.get("assets") or []
            out.append({"tag_name": rel.get("tag_name", ""),
                        "id": rel.get("id"),
                        "n_assets": len(assets),
                        "assets": [(a.get("name", ""),
                                    a.get("browser_download_url", ""))
                                   for a in assets]})
        if len(batch) < 100:
            break
    return out


def _load_index(releases):
    """构建 附件名 -> 直链 映射，供上传前做幂等复用。"""
    global _asset_index
    _asset_index = {}
    for rel in releases:
        for name, url in rel.get("assets", []):
            if name and url:
                _asset_index.setdefault(name, url)


def _tag_index(tag: str):
    """works-3 -> 3；不属于本前缀返回 None。"""
    if not tag.startswith(TAG_PREFIX):
        return None
    try:
        return int(tag[len(TAG_PREFIX):])
    except ValueError:
        return None


def _create_release(idx: int):
    """创建 works-<idx> release，成功返回 (tag, id)。"""
    tag = f"{TAG_PREFIX}{idx}"
    try:
        r = requests.post(f"{API}/repos/{_REPO}/releases", headers=_headers(),
                          json={"tag_name": tag, "name": f"作品归档 {tag}",
                                "body": "抖音监控自动归档的作品媒体文件。"},
                          timeout=30)
        if r.status_code in (200, 201):
            return tag, r.json().get("id")
        # 已存在（422）则直接取回
        if r.status_code == 422:
            r2 = requests.get(f"{API}/repos/{_REPO}/releases/tags/{tag}",
                              headers=_headers(), timeout=30)
            if r2.status_code == 200:
                return tag, r2.json().get("id")
        print(f"    !! 创建 release {tag} 失败: HTTP {r.status_code} "
              f"{r.text[:200]}")
    except Exception as e:
        print(f"    !! 创建 release {tag} 异常: {e}")
    return None, None


def ensure_release():
    """确定当前要写入的 release：复用最后一个未满的，否则新建。"""
    global _cur_tag, _cur_id, _cur_count, _ready
    if _ready:
        return _cur_id is not None
    all_rels = _list_releases()
    _load_index(all_rels)
    mine = [r for r in all_rels if _tag_index(r["tag_name"]) is not None]
    if mine:
        mine.sort(key=lambda r: _tag_index(r["tag_name"]))
        last = mine[-1]
        if last["n_assets"] < MAX_ASSETS:
            _cur_tag = last["tag_name"]
            _cur_id = last["id"]
            _cur_count = last["n_assets"]
            _ready = True
            return True
        next_idx = _tag_index(last["tag_name"]) + 1
    else:
        next_idx = 1
    tag, rid = _create_release(next_idx)
    if rid:
        _cur_tag, _cur_id, _cur_count = tag, rid, 0
        _ready = True
        return True
    return False


def _next_release():
    """当前 release 已满，新建下一个并切换过去。"""
    global _cur_tag, _cur_id, _cur_count
    idx = (_tag_index(_cur_tag) or 0) + 1
    tag, rid = _create_release(idx)
    if rid:
        _cur_tag, _cur_id, _cur_count = tag, rid, 0
        return True
    return False


def public_url(asset_name: str) -> str:
    """拼装 release 附件直链（作为 API 未返回时的兜底）。"""
    return (f"https://github.com/{_REPO}/releases/download/"
            f"{urllib.parse.quote(_cur_tag, safe='')}/"
            f"{urllib.parse.quote(asset_name, safe='')}")


def upload(path: Path, key: str, retries: int = 3) -> str:
    """上传单个文件到 release，成功返回公开直链，失败返回空串。

    key 形如 '昵称/时间_文案/01.jpg'，这里把 '/' 压平成 '__' 作为附件名
    （release 附件名不允许含路径分隔符）。
    """
    global _cur_count
    # 注意: 这里不能用 enabled(), 它会 reload() 把 _ready 重置为 False,
    # 导致每个文件都重新列一遍全部 release 附件(几百次多余 API 调用)。
    if not _TOKEN:
        reload()
    if not (_TOKEN and _REPO):
        return ""
    try:
        if path.stat().st_size > MAX_FILE:
            print(f"    !! 文件超过 2GiB 上限，跳过: {key}")
            return ""
    except Exception:
        return ""
    if not ensure_release():
        return ""
    asset_name = key.replace("/", "__")
    # 幂等: 同名附件此前已上传过（回灌历史作品时会大量遇到），直接复用已有直链
    if asset_name in _asset_index:
        print(f"    [store] 附件已存在, 复用直链: {asset_name}")
        return _asset_index[asset_name]
    for attempt in range(1, retries + 1):
        if _cur_count >= MAX_ASSETS and not _next_release():
            return ""
        try:
            with open(path, "rb") as f:
                r = requests.post(
                    f"{UPLOAD_API}/repos/{_REPO}/releases/{_cur_id}/assets",
                    headers={**_headers(),
                             "Content-Type": "application/octet-stream"},
                    params={"name": asset_name}, data=f, timeout=180)
            if r.status_code in (200, 201):
                _cur_count += 1
                url = (r.json().get("browser_download_url")
                       or public_url(asset_name))
                _asset_index[asset_name] = url
                return url
            print(f"    !! GH release 上传失败(第{attempt}次) {asset_name}: "
                  f"HTTP {r.status_code} {r.text[:150]}")
        except Exception as e:
            print(f"    !! GH release 上传异常(第{attempt}次) {asset_name}: {e}")
        time.sleep(2 * attempt)
    return ""
