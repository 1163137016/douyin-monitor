#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地→云端直传（复用 gh_release_store.py 上传到 GitHub Releases）。

职责
----
1) upload_new_media(): 扫描 D:\\gitdy，把还没上过云的每个媒体文件直接上传到
   本仓库的 GitHub Release 附件（与 GitHub Actions 用的是同一条链路），并在
   D:\\gitdy\\.cloud_manifest.json 记录 本地相对路径 -> 云端直链，做到幂等、可续传。
2) push_index(index_dict): 把画廊索引 gallery.json 推送到仓库（GitHub Contents API），
   即「往 GitHub 推一份索引」。
3) trigger_pages_rebuild(): 手动触发云端 Actions 重建并部署 GitHub Pages（best-effort）。

配置（放进 .env，不要泄露给任何人）
  GITHUB_TOKEN       一个有 repo 权限的 PAT（classic 的 repo 作用域，或 fine-grained 的
                     Contents+Pages+Actions 权限）。本地没法用 Actions 自带的 GITHUB_TOKEN，
                     必须单独准备一个 PAT。
  GITHUB_REPOSITORY  形如 owner/repo。若留空，会用 GITHUB_TOKEN 自动查找含 monitor.yml
                     的那个仓库（按仓库名含 douyin/monitor 猜测，再验证）。

未配置 GITHUB_TOKEN 时，所有函数安全返回「未启用」，本地服务器退回纯本地模式。
依赖: requests（与 monitor_run.py 同款）。
"""
import os
import io
import json
import time
import base64
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

import gh_release_store as gh

API = "https://api.github.com"
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
VID_EXT = (".mp4", ".webm", ".mov", ".m4v")
MEDIA_EXT = IMG_EXT + VID_EXT


def _cache_dir() -> Path:
    return Path(os.environ.get("CACHE_DIR", r"D:\gitdy")).resolve()


def _manifest_path() -> Path:
    return _cache_dir() / ".cloud_manifest.json"


def _status_path() -> Path:
    return _cache_dir() / ".cloud_status.json"


# --------------------------------------------------------------------------
# 本地清单：relpath -> {"url","size","uploaded_at"}
# --------------------------------------------------------------------------
def load_manifest():
    p = _manifest_path()
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {}


def save_manifest(m):
    try:
        _manifest_path().write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def load_status():
    p = _status_path()
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"enabled": False}


def save_status(s):
    try:
        _status_path().write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 仓库解析（显式 > 自动发现）
# --------------------------------------------------------------------------
def _headers(token):
    return {"Authorization": f"token {token}",
            "Accept": "application/vnd.github+json"}


def ensure_repo():
    """返回 (repo, token)。token 缺失返回 (None, None)；token 在但 repo 未指定则自动发现。"""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return None, None
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo and "/" in repo:
        return repo, token
    if requests is None:
        return None, token
    # 自动发现：优先仓库名含 douyin/monitor，再验证确有 monitor.yml
    try:
        for page in range(1, 6):
            r = requests.get(f"{API}/user/repos",
                             headers=_headers(token),
                             params={"per_page": 100, "page": page, "affiliation": "owner"},
                             timeout=30)
            if r.status_code != 200:
                break
            repos = r.json()
            if not repos:
                break
            for rp in repos:
                full = rp.get("full_name", "")
                low = full.lower()
                if "douyin" not in low and "monitor" not in low and "tiktok" not in low:
                    continue
                wf = requests.get(f"{API}/repos/{full}/contents/.github/workflows/monitor.yml",
                                 headers=_headers(token), timeout=20)
                if wf.status_code == 200:
                    os.environ["GITHUB_REPOSITORY"] = full
                    return full, token
            if len(repos) < 100:
                break
    except Exception:
        pass
    return None, token


def _default_branch(repo, token):
    try:
        r = requests.get(f"{API}/repos/{repo}", headers=_headers(token), timeout=20)
        if r.status_code == 200:
            return r.json().get("default_branch", "main")
    except Exception:
        pass
    return "main"


# --------------------------------------------------------------------------
# 云端索引：只用云端直链构建（绝不把本地 /media 路径推上去）
# --------------------------------------------------------------------------
def build_cloud_index():
    """扫描本地缓存，用 manifest 里的云端直链拼一份 gallery.json 结构。

    与 build_gallery.py 导出的 schema 保持一致，但 items[].url 一律是
    GitHub Releases 直链——这样推到仓库的索引在云端/别人机器上也能用。
    尚未上传成功的文件会被跳过（它们等下一轮上传后再入索引）。
    """
    manifest = load_manifest()
    cache = _cache_dir()
    accounts = []
    total = 0
    for acc in sorted(cache.iterdir()):
        if not acc.is_dir():
            continue
        works = []
        for post in sorted(acc.iterdir()):
            if not post.is_dir():
                continue
            items = []
            caption = ""
            for f in sorted(post.iterdir()):
                if f.name.lower() == "文案.txt":
                    try:
                        caption = f.read_text("utf-8", errors="ignore")
                    except Exception:
                        pass
                    continue
                ext = f.suffix.lower()
                if ext not in MEDIA_EXT:
                    continue
                rel = f.relative_to(cache).as_posix()
                url = (manifest.get(rel) or {}).get("url") or ""
                if not url:
                    continue
                items.append({"type": "video" if ext in VID_EXT else "image",
                              "url": url})
            if not items:
                continue
            pub, desc, link = _parse_caption(caption)
            has_v = any(i["type"] == "video" for i in items)
            has_i = any(i["type"] == "image" for i in items)
            works.append({
                "pub": pub,
                "desc": desc,
                "caption": caption.strip(),
                "kind": "video" if (has_v and not has_i) else ("live" if (has_v and has_i) else "image"),
                "items": items,
                "link": link,
            })
            total += 1
        if works:
            works.sort(key=lambda w: w.get("pub", ""), reverse=True)
            accounts.append({"id": "", "nick": acc.name, "works": works})
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": total, "source": "local->github-releases",
            "accounts": accounts}


def _parse_caption(text):
    """从 文案.txt 抽 发布时间 / 文案 / 作品链接（与 local_server 同规则）。"""
    pub = desc = link = ""
    if not text:
        return pub, desc, link
    lines = [ln.rstrip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("发布时间:"):
            pub = s.split(":", 1)[1].strip()
        elif s.startswith("作品链接:"):
            link = s.split(":", 1)[1].strip()
        elif s.startswith("文案:"):
            desc = s.split(":", 1)[1].strip()
            if not desc:
                for nxt in lines[i + 1:]:
                    if nxt.strip() and not nxt.strip().endswith(":"):
                        desc = nxt.strip()
                        break
    return pub, desc, link


def _index_urls(repo):
    """候选索引地址：Pages 线上索引（全量）> 仓库根 gallery.json。"""
    out = []
    custom = os.environ.get("CLOUD_INDEX_URL", "").strip()
    if custom:
        out.append(custom)
    owner, _, name = repo.partition("/")
    out.append(f"https://{owner}.github.io/{name}/gallery.json")
    return out


def fetch_remote_index():
    """读取云端现有 gallery.json（优先 Pages 线上的全量索引）。

    依次尝试 CLOUD_INDEX_URL > https://<owner>.github.io/<repo>/gallery.json
    > 仓库根的 gallery.json；全部失败返回 None。
    """
    repo, token = ensure_repo()
    if not repo:
        return None
    if requests is None:
        return None
    for url in _index_urls(repo):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                d = r.json()
                if isinstance(d, dict) and d.get("accounts"):
                    d["_source_url"] = url
                    return d
        except Exception:
            pass
    # 兜底：仓库里的 gallery.json
    if not token:
        return None
    try:
        r = requests.get(f"{API}/repos/{repo}/contents/gallery.json",
                         headers=_headers(token), timeout=30)
        if r.status_code != 200:
            return None
        raw = base64.b64decode(r.json().get("content", ""))
        d = json.loads(raw.decode("utf-8", errors="ignore"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _pub_variants(pub):
    """把发布时间转成若干「等价键」，抵消 UTC / 北京时间（±8h）的表示差异。

    云端 Actions 记录的是 UTC，本地缓存写的是北京时间，同一条作品会差 8 小时；
    只用原始字符串去重会产生重复作品。这里把 h、h±8 三种小时数都作为候选键。
    """
    s = (pub or "").strip()
    if not s:
        return {""}
    try:
        t = time.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return {s}
    base = int(time.mktime(t) // 3600)
    return {str(base + d) for d in (-8, 0, 8)}


def _work_keys(nick, w):
    """作品去重键集合：昵称 + 文案 + 发布时间（含 ±8h 容差）。"""
    desc = (w.get("desc") or "").strip()
    return {(nick, desc, v) for v in _pub_variants(w.get("pub"))}


def merge_indexes(remote, local):
    """以远端为主，把本地新增作品并进对应账号；时区/文案差异都能正确去重。"""
    if not remote:
        local.pop("_source_url", None)
        return local
    out = json.loads(json.dumps(remote))  # 深拷贝
    out.pop("_source_url", None)
    acc_map = {a.get("nick"): a for a in out.get("accounts", [])}
    seen = set()
    for a in out.get("accounts", []):
        for w in a.get("works", []):
            seen |= _work_keys(a.get("nick"), w)
    added = 0
    for la in local.get("accounts", []):
        nick = la.get("nick")
        if nick not in acc_map:
            acc_map[nick] = {"id": la.get("id", ""), "nick": nick, "works": []}
            out.setdefault("accounts", []).append(acc_map[nick])
        target = acc_map[nick]
        target.setdefault("works", [])
        for w in la.get("works", []):
            keys = _work_keys(nick, w)
            if keys & seen:
                continue
            target["works"].append(w)
            seen |= keys
            added += 1
    out["total"] = sum(len(a.get("works", [])) for a in out.get("accounts", []))
    out["generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out["local_added"] = added
    return out


def is_enabled():
    repo, token = ensure_repo()
    return bool(repo and token)


_PERM_CACHE = {"ts": 0.0, "ok": None, "reason": ""}


def check_write_permission(force=False):
    """探测令牌是否具备写权限（Contents: Read and write）。

    做法：往仓库写一个 1 字节的探针文件再删掉（可逆、无副作用）。
    结果缓存 10 分钟，避免每次状态刷新都打 API。
    返回 {"ok": True/False, "reason": "..."}；未配置令牌时 ok=False。
    """
    repo, token = ensure_repo()
    if not (repo and token):
        return {"ok": False, "reason": "未配置 GITHUB_TOKEN"}
    if requests is None:
        return {"ok": False, "reason": "缺少 requests 库"}
    now = time.time()
    if not force and _PERM_CACHE["ok"] is not None and now - _PERM_CACHE["ts"] < 600:
        return {"ok": _PERM_CACHE["ok"], "reason": _PERM_CACHE["reason"]}

    H = _headers(token)
    probe = "_sync_perm_probe.json"
    url = f"{API}/repos/{repo}/contents/{probe}"
    ok, reason = False, ""
    try:
        r = requests.put(url, headers=H, timeout=30,
                         json={"message": "perm probe", "content": "e30="})
        if r.status_code in (200, 201):
            ok = True
            try:
                sha = r.json()["content"]["sha"]
                requests.delete(url, headers=H, timeout=20,
                                json={"message": "perm probe cleanup", "sha": sha})
            except Exception:
                pass
        elif r.status_code == 403:
            reason = ("令牌只有读权限，需要给它加 Contents: Read and write"
                      "（GitHub → Settings → Developer settings → "
                      "Personal access tokens → 编辑该令牌 → Permissions）")
        else:
            reason = f"HTTP {r.status_code} {r.text[:160]}"
    except Exception as e:
        reason = f"请求异常: {e}"
    _PERM_CACHE.update({"ts": now, "ok": ok, "reason": reason})
    return {"ok": ok, "reason": reason}


# --------------------------------------------------------------------------
# 1) 本地→云端直传：上传未备份的媒体
# --------------------------------------------------------------------------
def upload_new_media(max_per_run=50, budget_min=20):
    repo, token = ensure_repo()
    if not (repo and token):
        return {"ok": False, "reason": "未配置 GITHUB_TOKEN（在 .env 填入有 repo 权限的 PAT）", "uploaded": 0}
    if requests is None:
        return {"ok": False, "reason": "缺少 requests 库", "uploaded": 0}
    gh.reload()  # 读取最新 env
    if not gh.enabled():
        return {"ok": False, "reason": "gh_release_store 不可用（缺 token/repo）", "uploaded": 0}
    # 先探一次写权限：只有读权限时不值得逐个文件撞 403
    perm = check_write_permission()
    if not perm["ok"]:
        return {"ok": False, "reason": perm["reason"], "uploaded": 0,
                "permission_error": True}
    gh.ensure_release()
    manifest = load_manifest()
    cache = _cache_dir()
    uploaded = skipped = errors = 0
    started = time.time()
    for acc in sorted(cache.iterdir()):
        if not acc.is_dir():
            continue
        for post in sorted(acc.iterdir()):
            if not post.is_dir():
                continue
            for f in sorted(post.iterdir()):
                if f.suffix.lower() not in MEDIA_EXT:
                    continue
                if uploaded >= max_per_run:
                    break
                if time.time() - started > budget_min * 60:
                    break
                rel = f.relative_to(cache).as_posix()
                size = f.stat().st_size
                rec = manifest.get(rel)
                if rec and rec.get("size") == size and rec.get("url"):
                    skipped += 1
                    continue
                url = gh.upload(f, rel)  # 内部已做幂等复用（同名附件命中直链）
                if url:
                    manifest[rel] = {"url": url, "size": size,
                                     "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    uploaded += 1
                else:
                    errors += 1
            if uploaded >= max_per_run or (time.time() - started > budget_min * 60):
                break
        if uploaded >= max_per_run or (time.time() - started > budget_min * 60):
            break
    save_manifest(manifest)
    return {"ok": True, "repo": repo, "uploaded": uploaded,
            "skipped": skipped, "errors": errors,
            "total_in_cloud": len(manifest)}


# --------------------------------------------------------------------------
# 2) 往 GitHub 推一份索引 gallery.json
# --------------------------------------------------------------------------
def push_index(index_dict, message="chore: 更新云端索引 gallery.json"):
    repo, token = ensure_repo()
    if not (repo and token) or requests is None:
        return {"ok": False, "reason": "未配置云端"}
    content = json.dumps(index_dict, ensure_ascii=False, indent=2).encode("utf-8")
    b64 = base64.b64encode(content).decode("ascii")
    url = f"{API}/repos/{repo}/contents/gallery.json"
    headers = _headers(token)
    sha = None
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    br = _default_branch(repo, token)
    payload = {"message": message, "content": b64, "branch": br}
    if sha:
        payload["sha"] = sha
    try:
        rp = requests.put(url, headers=headers, json=payload, timeout=40)
    except Exception as e:
        return {"ok": False, "reason": f"请求异常: {e}"}
    if rp.status_code in (200, 201):
        return {"ok": True,
                "url": f"https://github.com/{repo}/blob/{br}/gallery.json",
                "size_kb": round(len(content) / 1024, 1)}
    if rp.status_code == 403:
        return {"ok": False,
                "reason": "令牌缺少写权限（Contents: Read and write），无法推送索引"}
    return {"ok": False, "reason": f"HTTP {rp.status_code} {rp.text[:160]}"}


# --------------------------------------------------------------------------
# 3) 手动触发云端 Actions 重建并部署 Pages（best-effort，非自动）
# --------------------------------------------------------------------------
def trigger_pages_rebuild():
    repo, token = ensure_repo()
    if not (repo and token) or requests is None:
        return {"ok": False, "reason": "未配置云端"}
    br = _default_branch(repo, token)
    url = f"{API}/repos/{repo}/actions/workflows/monitor.yml/dispatches"
    try:
        r = requests.post(url, headers=_headers(token), json={"ref": br}, timeout=20)
    except Exception as e:
        return {"ok": False, "reason": f"请求异常: {e}"}
    if r.status_code in (200, 204):
        return {"ok": True, "msg": "已触发云端重建画廊（Actions 会重新生成并部署 Pages）"}
    return {"ok": False, "reason": f"HTTP {r.status_code} {r.text[:160]}"}


# --------------------------------------------------------------------------
# 编排：上传 + 推索引，写状态文件
# --------------------------------------------------------------------------
def cloud_sync_once(index=None, max_per_run=50):
    repo, token = ensure_repo()
    if not (repo and token):
        st = {"enabled": False,
              "reason": "未配置 GITHUB_TOKEN（在 .env 填入有 repo 权限的 PAT 即自动开启）",
              "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_status(st)
        return st
    up = upload_new_media(max_per_run=max_per_run)
    perm = check_write_permission()
    st = {"enabled": True, "repo": repo, "permission": perm, "last_upload": up,
          "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if perm.get("ok"):
        # 索引一律用云端直链，并与远端已有索引合并（不覆盖 Actions 生成的内容）
        local_idx = index if index is not None else build_cloud_index()
        try:
            merged = merge_indexes(fetch_remote_index(), local_idx)
        except Exception:
            merged = local_idx
        st["last_push"] = push_index(merged)
        st["indexed"] = merged.get("total")
    else:
        st["last_push"] = {"ok": False, "reason": "无写权限，跳过索引推送"}
    save_status(st)
    return st
