#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音监控 · 本地服务器（把电脑变成服务器）

职责
----
1) 后台常驻：每隔一段时间（默认 30 分钟，开机即启动）调用 monitor_run.py
   把各账号的新作品下载缓存到 D:\\gitdy（STORAGE_BACKEND=local，不上传云端，
   云端仍由你已配好的 GitHub Actions 负责，本地只做缓存+服务）。
2) 扫描 D:\\gitdy 构建画廊索引，提供精美、手机/电脑自适应的网页。
3) 媒体走 /media 本地直读并支持 Range，视频可拖动、秒开（不再慢吞吞连 GitHub）。
4) 可选公网访问：设 EXPOSE_PUBLIC=1 时自动尝试用 cloudflared 开一条临时隧道，
   手机用流量也能看（URL 每次重启会变；想要固定地址需自备域名+Cloudflare）。
5) 本地→云端直传（可选，默认自动开启）：监控下载后自动把新作品上传到
   GitHub Releases（复用 gh_release_store.py，与 GitHub Actions 同一链路），并推送
   gallery.json 索引到仓库；网页可手动「刷新云端画廊」。本地文件即使被删，画廊也能
   回退到云端直链（本地+云端双保险）。
   需要 .env 里配置 GITHUB_TOKEN（有 repo 权限的 PAT）。未配置则自动退回纯本地模式。

访问地址
--------
- 本机:        http://localhost:PORT
- 同 WiFi 手机: http://<电脑内网IP>:PORT
- 公网(若开):  启动日志/状态页里给出的 *.trycloudflare.com 地址

配置（环境变量，均可不填用默认值）
  CACHE_DIR             本地缓存根目录, 默认 D:\\gitdy
  PORT                 网页端口, 默认 8000
  HOST                 绑定地址, 默认 0.0.0.0(允许手机/公网访问)
  MONITOR_INTERVAL_MIN 监控间隔(分钟), 默认 30
  EXPOSE_PUBLIC        是否开公网隧道: 1/true 开启
  CLOUD_SYNC           本地→云端直传开关: 1/true 强制开, 0/off 强制关, 留空/auto 则『有 PAT 就开』
  CLOUD_MAX_PER_RUN    每轮最多上传几个文件(防突发), 默认 50
  GITHUB_TOKEN         有 repo 权限的 PAT（云端直传必须）
  GITHUB_REPOSITORY    owner/repo（留空则用 PAT 自动发现含 monitor.yml 的仓库）
  MONITOR_SCRIPT       monitor_run.py 路径(默认同目录)
依赖: 仅标准库(http.server/threading 等); 监控子进程需要 monitor_run.py 的依赖(curl_cffi 等)。
"""
import os
import re
import sys
import io
import json
import time
import shutil
import socket
import hashlib
import subprocess
import threading
import mimetypes
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# 保证同目录的 cloud_sync 等模块可被导入（部分环境下 sys.path[0] 不是脚本目录）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_sync  # 本地→云端直传 / 推送索引（未配置 GITHUB_TOKEN 时安全降级为纯本地）

ROOT = Path(__file__).resolve().parent
CACHE_DIR = Path(os.environ.get("CACHE_DIR", r"D:\gitdy")).resolve()
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
INTERVAL = max(1, int(os.environ.get("MONITOR_INTERVAL_MIN", "30")))
MONITOR_SCRIPT = Path(os.environ.get("MONITOR_SCRIPT", ROOT / "monitor_run.py")).resolve()
EXPOSE_PUBLIC = str(os.environ.get("EXPOSE_PUBLIC", "")).strip().lower() in ("1", "true", "yes")
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# 载入 .env（与 monitor_run.py 同款逻辑，确保 COOKIE / ACCOUNTS_URL 等可用）
# ---------------------------------------------------------------------------
def _load_local_env():
    ep = ROOT / ".env"
    if not ep.exists():
        return
    for ln in ep.read_text("utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_local_env()
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 本地→云端直传（复用 gh_release_store 上传到 GitHub Releases + 推送 gallery.json 索引）。
# 默认“自动”：只要 .env 里有 GITHUB_TOKEN（有 repo 权限的 PAT）就开启；显式设 CLOUD_SYNC=0/off 可强制关闭。
_cloud_env = str(os.environ.get("CLOUD_SYNC", "")).strip().lower()
CLOUD_SYNC = (_cloud_env in ("1", "true", "yes", "on")
              or (_cloud_env in ("", "auto") and bool(os.environ.get("GITHUB_TOKEN", "").strip())))
CLOUD_MAX_PER_RUN = max(1, int(os.environ.get("CLOUD_MAX_PER_RUN", "50")))

# 云端画廊索引（Pages 上的 gallery.json）：把「只在云端」的作品也补进本地网页。
CLOUD_CATALOG_TTL = max(60, int(os.environ.get("CLOUD_CATALOG_TTL", "600")))
CLOUD_PROXY_MAX = max(1, int(os.environ.get("CLOUD_PROXY_MAX_MB", "150"))) * 1024 * 1024
CLOUD_CACHE_DIR = CACHE_DIR / ".cloudcache"
# 只允许代理这些主机的媒体（防止被当成任意 URL 跳板）
_CLOUD_ALLOW_HOSTS = ("github.com", "objects.githubusercontent.com",
                      "release-assets.githubusercontent.com", "raw.githubusercontent.com")
_cat_cache = {"ts": 0.0, "accounts": None}
_dl_locks = {}
_dl_guard = threading.Lock()

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
VID_EXT = (".mp4", ".webm", ".mov", ".m4v")

# 同时写控制台与 D:\gitdy\server.log（无界面自启时也能查日志）
def plog(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(CACHE_DIR / "server.log", "a", encoding="utf-8") as _f:
            _f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 监控线程：周期性调用 monitor_run.py（本地缓存模式）
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.last_run = None
        self.last_status = "等待首次运行"
        self.next_run = None
        self.force = False
        self.log = []          # 最近若干行监控输出
        self.public_url = ""
        self._stop = False
        self._idx_cache = None
        self._idx_time = 0

    # ---- 运行一次 monitor_run.py（STORAGE_BACKEND=local） ----
    def run_once(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.last_status = "监控中…"
        try:
            env = dict(os.environ)
            env["STORAGE_BACKEND"] = "local"          # 不导入远端存储, 只缓存到本地
            env["DOWNLOAD_DIR"] = str(CACHE_DIR)       # 缓存根目录 = D:\gitdy
            env["KEEP_LOCAL"] = "1"                    # 绝不允许删本地缓存
            # 关键：清掉令牌，否则 monitor_run 会继承 .env 的 PAT -> 上传后删本地文件
            env["GITHUB_TOKEN"] = ""
            env["GITHUB_REPOSITORY"] = ""
            env["PYTHONUNBUFFERED"] = "1"
            self._log(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动监控子进程 =====")
            proc = subprocess.Popen(
                [PYTHON, str(MONITOR_SCRIPT)],
                env=env, cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore", bufsize=1,
            )
            for line in proc.stdout:
                self._log(line.rstrip("\n"))
            proc.wait()
            self.last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self.last_status = "空闲（等待下一轮）" if proc.returncode == 0 else f"上次异常(退出码 {proc.returncode})"
            self._log(f"===== 监控结束 退出码={proc.returncode} =====")
            # 监控下载完成后，自动把新作品直传到云端 + 推送索引
            if proc.returncode == 0 and CLOUD_SYNC:
                try:
                    self._log("===== 开始本地→云端直传 =====")
                    cs = cloud_sync.cloud_sync_once(None, max_per_run=CLOUD_MAX_PER_RUN)
                    up = cs.get("last_upload", {})
                    line = f"[云端] 本次上传 {up.get('uploaded', 0)} 个，云端已有 {up.get('total_in_cloud', 0)} 个"
                    if cs.get("last_push"):
                        line += "，索引推送: " + ("成功" if cs["last_push"].get("ok") else "未推送")
                    self._log(line)
                except Exception as e:
                    self._log(f"[云端] 同步异常: {e}")
        except Exception as e:
            self.last_status = f"启动失败: {e}"
            self._log(f"!! 监控子进程异常: {e}")
        finally:
            with self.lock:
                self.running = False
                self._idx_cache = None  # 索引失效, 下次请求重建

    def _log(self, line):
        self.log.append(line)
        if len(self.log) > 400:
            self.log = self.log[-400:]

    def trigger(self):
        with self.lock:
            if self.running:
                return False
        threading.Thread(target=self.run_once, daemon=True).start()
        return True

    def loop(self):
        self.next_run = time.time() + 5
        while not self._stop:
            now = time.time()
            due = self.force or (self.next_run and now >= self.next_run)
            with self.lock:
                busy = self.running
            if due and not busy:
                self.force = False
                self.run_once()
                self.next_run = time.time() + INTERVAL * 60
            else:
                time.sleep(5)


monitor = Monitor()


# ---------------------------------------------------------------------------
# 索引构建：扫描 D:\gitdy 目录结构
#   D:\gitdy\<账号昵称>\ <作品文件夹>\ {01.jpg, 02.jpg, 视频.mp4, 文案.txt}
# ---------------------------------------------------------------------------
def _parse_caption(text):
    """从 文案.txt 抽取 发布时间 / 文案 / 作品链接。

    文件结构:
        发布时间: ...
        作品类型: ...
        (空行)
        文案:
        <真实文案，可能多行>
        (空行)
        点赞 ... | 评论 ... | 收藏 ... | 分享 ...
        作品链接: ...
    """
    pub, desc, link = "", "", ""
    if not text:
        return pub, desc, link
    capture = False
    cap = []
    for ln in text.splitlines():
        s = ln.strip()
        if ln.startswith("发布时间:"):
            pub = ln.split(":", 1)[1].strip()
        elif ln.startswith("作品链接:"):
            link = ln.split(":", 1)[1].strip()
        elif ln.startswith("文案:"):
            after = ln.split(":", 1)[1].strip()
            if after:                 # 文案与内容同行
                cap = [after]
                capture = False
            else:                    # 内容在下一行
                capture = True
            continue
        if capture:
            if s.startswith(("点赞", "作品链接", "发布时间", "作品类型")):
                break
            if s == "":
                if cap:
                    break
                continue
            cap.append(s)
    desc = " ".join(cap).strip()
    return pub, desc, link


def cloud_catalog(force=False):
    """读取线上 gallery.json（Pages 优先，其次 CLOUD_INDEX_URL），返回 accounts 列表。

    结果缓存 CLOUD_CATALOG_TTL 秒（默认 10 分钟），避免每次刷新页面都去打 GitHub。
    """
    now = time.time()
    if (not force and _cat_cache["accounts"] is not None
            and now - _cat_cache["ts"] < CLOUD_CATALOG_TTL):
        return _cat_cache["accounts"]
    urls = []
    custom = os.environ.get("CLOUD_INDEX_URL", "").strip()
    if custom:
        urls.append(custom)
    try:
        repo = cloud_sync.ensure_repo()[0]
    except Exception:
        repo = None
    if repo:
        owner, _, name = repo.partition("/")
        urls.append(f"https://{owner}.github.io/{name}/gallery.json")
    accounts = []
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
            if not raw.lstrip().startswith(b"{"):
                continue
            d = json.loads(raw.decode("utf-8", errors="ignore"))
            accounts = d.get("accounts") or []
            if accounts:
                break
        except Exception:
            continue
    _cat_cache["accounts"] = accounts
    _cat_cache["ts"] = now
    return accounts


def _cloud_proxy_url(u):
    """云端直链 -> 本地代理地址（由本机下载后按正确 MIME 输出，避免 octet-stream）。"""
    return "/cloud?u=" + urllib.parse.quote(u, safe="")


def _ext_of_url(u):
    p = urllib.parse.urlparse(u).path
    ext = os.path.splitext(p)[1].lower()
    return ext if ext in (IMG_EXT + VID_EXT) else ""


def fetch_cloud_to_cache(u, timeout=180):
    """把云端媒体下载到 CLOUD_CACHE_DIR，返回本地路径；失败返回 None。

    同一个 URL 只下载一次（按 URL 哈希命名 + 进程内锁去重），之后秒开。
    """
    ext = _ext_of_url(u)
    key = hashlib.sha1(u.encode("utf-8")).hexdigest()
    dst = CLOUD_CACHE_DIR / key[:2] / (key + (ext or ".bin"))
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    with _dl_guard:
        lock = _dl_locks.setdefault(key, threading.Lock())
    with lock:
        if dst.exists() and dst.stat().st_size > 0:  # 等锁期间别人已经下好
            return dst
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".part")
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                clen = int(r.headers.get("Content-Length") or 0)
                if clen and clen > CLOUD_PROXY_MAX:
                    plog(f"[云端代理] 文件过大({clen // 1048576}MB)，改直连: {u[-40:]}")
                    return None
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f, 1024 * 256)
            if tmp.stat().st_size == 0:
                tmp.unlink(missing_ok=True)
                return None
            os.replace(tmp, dst)
            plog(f"[云端代理] 已缓存 {dst.stat().st_size // 1024}KB <- {u[-50:]}")
            return dst
        except Exception as e:
            try:
                (dst.with_name(dst.name + ".part")).unlink(missing_ok=True)
            except Exception:
                pass
            plog(f"[云端代理] 下载失败 ({e}) {u[-60:]}")
            return None
        finally:
            with _dl_guard:
                _dl_locks.pop(key, None)


def build_index():
    """返回 {updated, total, accounts:[{name,count,posts:[...]}]}。带 60s 内存缓存。"""
    now = time.time()
    if monitor._idx_cache and now - monitor._idx_time < 60:
        return monitor._idx_cache
    # 云端直链清单：本地文件被删时仍可回退到云端（双保险）
    try:
        _cloud_urls = {k: v.get("url") for k, v in cloud_sync.load_manifest().items() if v.get("url")}
    except Exception:
        _cloud_urls = {}
    accounts = []
    total = 0
    for acc in sorted(CACHE_DIR.iterdir()):
        if not acc.is_dir() or acc.name.startswith("."):
            continue  # 跳过 .cloudcache 等内部目录
        nick = acc.name
        posts = []
        for post in sorted(acc.iterdir()):
            if not post.is_dir():
                continue
            media = []
            caption = ""
            for f in post.iterdir():
                ext = f.suffix.lower()
                if ext in IMG_EXT or ext in VID_EXT:
                    rel = f"{nick}/{post.name}/{f.name}"
                    if f.exists():
                        url = f"/media/{urllib.parse.quote(nick)}/{urllib.parse.quote(post.name)}/{urllib.parse.quote(f.name)}"
                    else:
                        curl = _cloud_urls.get(rel)
                        if not curl:
                            continue
                        url = curl
                    media.append({"url": url, "kind": ("video" if ext in VID_EXT else "image"),
                                  "name": f.name, "cloud": url.startswith("http")})
                elif f.name.lower() == "文案.txt":
                    try:
                        caption = f.read_text("utf-8", errors="ignore")
                    except Exception:
                        pass
            if not media:
                # 本地文件可能已被删，但云端仍有备份 -> 用云端清单重建
                prefix = f"{nick}/{post.name}/"
                cloud_media = [(k, v) for k, v in _cloud_urls.items()
                               if k.startswith(prefix) and not k.lower().endswith("文案.txt")]
                if not cloud_media:
                    continue
                for k, url in cloud_media:
                    ext = os.path.splitext(k)[1].lower()
                    media.append({"url": url, "kind": ("video" if ext in VID_EXT else "image"),
                                  "name": k.rsplit("/", 1)[-1], "cloud": True})
            has_v = any(m["kind"] == "video" for m in media)
            has_i = any(m["kind"] == "image" for m in media)
            kind = "video" if (has_v and not has_i) else ("live" if (has_v and has_i) else "image")
            pub, desc, link = _parse_caption(caption)
            posts.append({
                "id": f"{nick}|{post.name}",
                "account": nick,
                "kind": kind,
                "desc": desc or post.name,
                "pub": pub,
                "link": link,
                "media": media,
            })
        if posts:
            total += len(posts)
            accounts.append({"name": nick, "count": len(posts), "posts": posts})

    # ---- 补全「只在云端」的作品：本地没缓存过，但云端画廊（Pages 索引）已有 ----
    local_total = total
    cloud_only = 0
    acc_index = {a["name"]: a for a in accounts}
    local_keys = set()
    for a in accounts:
        for p in a["posts"]:
            local_keys |= cloud_sync._work_keys(p["account"],
                                               {"desc": p["desc"], "pub": p["pub"]})
    try:
        cats = cloud_catalog()
    except Exception:
        cats = []
    for ca in cats:
        nick = (ca.get("nick") or "").strip()
        if not nick:
            continue
        for w in ca.get("works", []):
            desc = (w.get("desc") or w.get("caption") or "").strip()
            pub = w.get("pub") or ""
            keys = cloud_sync._work_keys(nick, {"desc": desc, "pub": pub})
            if keys & local_keys:
                continue
            items = [it for it in (w.get("items") or []) if it.get("url")]
            if not items:
                continue
            media = []
            for it in items:
                u = it["url"]
                media.append({"url": _cloud_proxy_url(u),
                              "kind": ("video" if it.get("type") == "video" else "image"),
                              "name": os.path.basename(urllib.parse.urlparse(u).path),
                              "cloud": True, "remote": u})
            has_v = any(m["kind"] == "video" for m in media)
            has_i = any(m["kind"] == "image" for m in media)
            kind = w.get("kind") or ("video" if (has_v and not has_i)
                                     else ("live" if (has_v and has_i) else "image"))
            if nick not in acc_index:
                acc_index[nick] = {"name": nick, "count": 0, "posts": []}
                accounts.append(acc_index[nick])
            node = acc_index[nick]
            node["posts"].append({
                "id": f"cloud|{nick}|{pub}|{len(node['posts'])}",
                "account": nick,
                "kind": kind,
                "desc": desc or "(无文案)",
                "pub": pub,
                "link": w.get("link") or "",
                "media": media,
                "cloud_only": True,
            })
            node["count"] = len(node["posts"])
            local_keys |= keys
            cloud_only += 1
    for a in accounts:
        a["posts"].sort(key=lambda p: p.get("pub") or "", reverse=True)
        a["count"] = len(a["posts"])
    accounts.sort(key=lambda a: a["name"])
    total = sum(a["count"] for a in accounts)

    result = {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "total": total,
              "local_total": local_total, "cloud_only": cloud_only,
              "accounts": accounts}
    monitor._idx_cache = result
    monitor._idx_time = now
    return result


# ---------------------------------------------------------------------------
# 公网隧道（best-effort）：cloudflared quick tunnel
# ---------------------------------------------------------------------------
def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def try_expose():
    if not EXPOSE_PUBLIC:
        return
    exe = shutil.which("cloudflared")
    if not exe:
        # 尝试常见安装位置
        for cand in (ROOT / "tools" / "cloudflared.exe",
                     Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
                     Path(r"C:\cloudflared\cloudflared.exe")):
            if cand.exists():
                exe = str(cand)
                break
    if not exe:
        monitor.public_url = "(未找到 cloudflared，公网访问不可用，详见说明)"
        return
    try:
        proc = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://localhost:{PORT}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="ignore", bufsize=1,
        )
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                monitor.public_url = m.group(0)
                monitor._log(f"[公网] 已开通: {monitor.public_url}")
                break
    except Exception as e:
        monitor.public_url = f"(隧道启动失败: {e})"


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
APP_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>抖音作品库 · 本地服务器</title>
<style>
  :root{
    --bg:#0c0e14; --panel:#141823; --panel2:#1b2030; --line:#262c3d;
    --txt:#e8ecf5; --sub:#8b93a7; --accent:#6d5efc; --accent2:#22d3ee;
    --live:#ff4d8d; --video:#22d3ee; --radius:16px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:radial-gradient(1200px 600px at 80% -10%,#1a1f33 0%,var(--bg) 55%);
    color:var(--txt);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
  a{color:inherit}
  .app{display:flex;min-height:100vh}
  /* 侧边栏 */
  .side{width:248px;flex:0 0 248px;background:linear-gradient(180deg,var(--panel),#10131c);
    border-right:1px solid var(--line);padding:18px 14px;position:sticky;top:0;height:100vh;overflow:auto}
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2));
    display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;box-shadow:0 6px 18px rgba(109,94,252,.45)}
  .brand b{font-size:16px;letter-spacing:.5px}
  .status{font-size:12px;color:var(--sub);margin:4px 0 14px;display:flex;align-items:center;gap:6px}
  .dot{width:8px;height:8px;border-radius:50%;background:#39d98a;box-shadow:0 0 8px #39d98a}
  .dot.busy{background:#ffb020;box-shadow:0 0 8px #ffb020}
  .search{width:100%;padding:10px 12px;border-radius:12px;border:1px solid var(--line);
    background:var(--panel2);color:var(--txt);outline:none;font-size:14px}
  .search:focus{border-color:var(--accent)}
  .accs{margin-top:14px;display:flex;flex-direction:column;gap:4px}
  .acc{padding:9px 11px;border-radius:11px;cursor:pointer;display:flex;justify-content:space-between;
    align-items:center;color:var(--sub);border:1px solid transparent}
  .acc:hover{background:var(--panel2)}
  .acc.on{background:linear-gradient(135deg,rgba(109,94,252,.22),rgba(34,211,238,.12));color:#fff;border-color:rgba(109,94,252,.5)}
  .acc .n{display:flex;align-items:center;gap:9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ava{width:24px;height:24px;border-radius:50%;flex:0 0 24px;display:flex;align-items:center;justify-content:center;
    font-size:12px;font-weight:700;color:#fff;background:linear-gradient(135deg,#6d5efc,#22d3ee)}
  .cnt{font-size:11px;background:var(--panel2);padding:1px 8px;border-radius:20px;color:var(--sub)}
  /* 主区 */
  .main{flex:1;padding:22px 26px;min-width:0}
  .top{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:16px}
  .top h1{font-size:20px;margin:0}
  .top .meta{color:var(--sub);font-size:13px;margin-top:3px}
  .acts{display:flex;gap:10px;flex-wrap:wrap}
  .btn{padding:9px 15px;border-radius:11px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);
    cursor:pointer;font-size:13px;display:inline-flex;align-items:center;gap:7px}
  .btn.primary{background:linear-gradient(135deg,var(--accent),#5a4bdb);border:none;color:#fff;box-shadow:0 6px 16px rgba(109,94,252,.4)}
  .btn:hover{filter:brightness(1.08)}
  .urlpill{font-size:12px;color:var(--accent2);background:rgba(34,211,238,.1);border:1px solid rgba(34,211,238,.3);
    padding:6px 11px;border-radius:20px;text-decoration:none;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .srcs{display:flex;gap:6px;margin-top:9px}
  .src{font-size:12px;padding:4px 12px;border-radius:20px;cursor:pointer;color:var(--sub);
    border:1px solid var(--line);background:var(--panel2);user-select:none}
  .src.on{color:#fff;border-color:rgba(109,94,252,.6);background:linear-gradient(135deg,rgba(109,94,252,.35),rgba(34,211,238,.18))}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;cursor:pointer;
    transition:transform .15s ease,box-shadow .15s ease;position:relative}
  .card:hover{transform:translateY(-4px);box-shadow:0 14px 30px rgba(0,0,0,.45)}
  .thumb{aspect-ratio:1/1;background:#0a0c12;display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative}
  .thumb img,.thumb video{width:100%;height:100%;object-fit:cover;display:block}
  .badge{position:absolute;top:8px;left:8px;font-size:11px;padding:3px 8px;border-radius:8px;font-weight:700;backdrop-filter:blur(4px)}
  .badge.live{background:rgba(255,77,141,.92);color:#fff}
  .badge.video{background:rgba(34,211,238,.9);color:#06222a}
  .badge.image{background:rgba(20,24,35,.8);color:#cfd6e6}
  .badge.cloud{left:auto;right:8px;background:rgba(109,94,252,.92);color:#fff}
  /* 云端作品占位（不自动下载，点了才拉） */
  .ph{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;
    background:linear-gradient(135deg,#171c2b,#0e1219);color:var(--sub)}
  .ph .phico{font-size:22px;opacity:.85}
  .ph .phtxt{font-size:11px;opacity:.75}
  .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
  .play span{width:46px;height:46px;border-radius:50%;background:rgba(0,0,0,.45);backdrop-filter:blur(3px);
    display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px}
  .more{position:absolute;right:8px;bottom:8px;font-size:11px;background:rgba(0,0,0,.55);color:#fff;padding:2px 7px;border-radius:8px}
  .cap{padding:9px 11px}
  .cap .d{font-size:13px;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cap .t{font-size:11px;color:var(--sub);margin-top:3px}
  .empty{color:var(--sub);text-align:center;padding:60px 20px}
  /* 灯箱 */
  .modal{position:fixed;inset:0;background:rgba(4,6,10,.86);backdrop-filter:blur(6px);display:none;
    align-items:center;justify-content:center;z-index:50;padding:24px}
  .modal.on{display:flex}
  .mbox{max-width:min(960px,94vw);max-height:92vh;background:var(--panel);border:1px solid var(--line);
    border-radius:18px;overflow:hidden;display:flex;flex-direction:column}
  .mstage{background:#000;display:flex;align-items:center;justify-content:center;max-height:68vh;min-height:240px;position:relative}
  .mstage img,.mstage video{max-width:100%;max-height:68vh;display:block}
  .loadhint{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    color:#cfd6e6;font-size:13px;background:rgba(0,0,0,.5);pointer-events:none;text-align:center;padding:20px}
  .mstrip{display:flex;gap:8px;padding:10px;overflow-x:auto;background:var(--panel2)}
  .mstrip img,.mstrip video{height:62px;border-radius:8px;object-fit:cover;cursor:pointer;opacity:.6;border:2px solid transparent}
  .mstrip .on{opacity:1;border-color:var(--accent)}
  .minfo{padding:14px 16px;max-height:24vh;overflow:auto}
  .minfo .desc{font-size:15px;margin-bottom:6px}
  .minfo .meta{color:var(--sub);font-size:12px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
  .minfo a{color:var(--accent2);text-decoration:none}
  .mclose{position:absolute;top:10px;right:12px;font-size:22px;color:#fff;cursor:pointer;z-index:2;background:rgba(0,0,0,.4);
    width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center}
  .mnav{position:absolute;top:50%;transform:translateY(-50%);font-size:26px;color:#fff;cursor:pointer;
    background:rgba(0,0,0,.4);width:42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center;user-select:none}
  .mnav.l{left:10px}.mnav.r{right:10px}
  @media(max-width:760px){
    .app{flex-direction:column}
    .side{width:100%;flex:none;height:auto;position:static;border-right:none;border-bottom:1px solid var(--line);padding:14px}
    .accs{flex-direction:row;overflow-x:auto;margin-top:10px;gap:8px}
    .acc{white-space:nowrap}
    .main{padding:16px}
    .grid{grid-template-columns:repeat(2,1fr);gap:10px}
  }
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand"><div class="logo">抖</div><b>抖音作品库</b></div>
    <div class="status"><span class="dot" id="dot"></span><span id="statusTxt">加载中…</span></div>
    <input class="search" id="search" placeholder="搜索文案 / 账号…" />
    <div class="accs" id="accs"></div>
  </aside>
  <main class="main">
    <div class="top">
      <div>
        <h1>我的收藏</h1>
        <div class="meta" id="meta">加载中…</div>
        <div class="meta" id="cloudMeta"></div>
        <div class="srcs" id="srcs">
          <span class="src on" data-src="all">全部</span>
          <span class="src" data-src="local">本地</span>
          <span class="src" data-src="cloud">云端</span>
        </div>
      </div>
      <div class="acts">
        <a class="urlpill" id="urlpill" style="display:none"></a>
        <button class="btn" id="cloudBtn">☁ 上传云端</button>
        <button class="btn" id="rebuildBtn">🔄 刷新云端画廊</button>
        <button class="btn primary" id="syncBtn">↻ 立即同步</button>
      </div>
    </div>
    <div class="grid" id="grid"></div>
    <div class="empty" id="empty" style="display:none">还没有缓存作品。<br/>点击右上角「立即同步」开始下载，或等待后台自动监控。</div>
  </main>
</div>

<div class="modal" id="modal">
  <div class="mbox">
    <div class="mclose" onclick="closeModal()">×</div>
    <div class="mstage" id="mstage"></div>
    <div class="mstrip" id="mstrip"></div>
    <div class="minfo" id="minfo"></div>
  </div>
</div>

<script>
let DATA=null, FILTER={acc:"__all__", q:"", src:"all"};
const $=s=>document.querySelector(s);
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function colorOf(n){let h=0;for(const c of n)h=(h*31+c.charCodeAt(0))%360;return `hsl(${h} 70% 55%)`;}

async function load(){
  try{
    const r=await fetch("/api/index"); DATA=await r.json();
  }catch(e){ DATA={accounts:[],total:0,updated:""}; }
  renderStatus();
  renderAccs();
  renderGrid();
  loadCloud();
}
async function loadStatus(){
  try{ const r=await fetch("/api/status"); const s=await r.json();
    $("#statusTxt").textContent=s.monitor_running?"监控同步中…":"空闲 · 上次 "+(s.last_run||"无");
    $("#dot").className="dot"+(s.monitor_running?" busy":"");
    const m=$("#meta");
    if(DATA){
      const lt=DATA.local_total??DATA.total, co=DATA.cloud_only??0;
      m.textContent=`共 ${DATA.total} 条作品 · 本地 ${lt} · 云端 ${co} · ${DATA.accounts.length} 个账号 · 索引更新 ${DATA.updated}`;
    }
    const pill=$("#urlpill");
    if(s.public_url && s.public_url.startsWith("http")){pill.style.display="";pill.href=s.public_url;pill.textContent="公网: "+s.public_url;}
  }catch(e){}
}
function renderStatus(){ loadStatus(); }
function renderAccs(){
  const box=$("#accs"); box.innerHTML="";
  const all=document.createElement("div");
  all.className="acc"+(FILTER.acc==="__all__"?" on":"");
  all.innerHTML=`<div class="n"><span class="ava" style="background:linear-gradient(135deg,#6d5efc,#22d3ee)">全</span>全部</div><span class="cnt">${DATA?DATA.total:0}</span>`;
  all.onclick=()=>{FILTER.acc="__all__";renderAccs();renderGrid();};
  box.appendChild(all);
  (DATA?DATA.accounts:[]).forEach(a=>{
    const d=document.createElement("div");
    d.className="acc"+(FILTER.acc===a.name?" on":"");
    const ini=esc(a.name.slice(0,1));
    d.innerHTML=`<div class="n"><span class="ava" style="background:${colorOf(a.name)}">${ini}</span>${esc(a.name)}</div><span class="cnt">${a.count}</span>`;
    d.onclick=()=>{FILTER.acc=a.name;renderAccs();renderGrid();};
    box.appendChild(d);
  });
}
function flatPosts(){
  let posts=[];
  (DATA?DATA.accounts:[]).forEach(a=>{
    a.posts.forEach(p=>{ if(FILTER.acc==="__all__"||p.account===FILTER.acc) posts.push(p); });
  });
  if(FILTER.src==="local") posts=posts.filter(p=>!p.cloud_only);
  if(FILTER.src==="cloud") posts=posts.filter(p=>p.cloud_only);
  if(FILTER.q){ const q=FILTER.q.toLowerCase();
    posts=posts.filter(p=>(p.desc||"").toLowerCase().includes(q)||(p.account||"").toLowerCase().includes(q)); }
  return posts;
}
function primary(p){
  if(p.kind==="image") return p.media[0];
  return p.media.find(m=>m.kind==="video")||p.media[0];
}
function renderGrid(){
  const grid=$("#grid"); grid.innerHTML="";
  const posts=flatPosts();
  $("#empty").style.display=posts.length?"none":"block";
  posts.forEach(p=>{
    const c=document.createElement("div"); c.className="card";
    const m=primary(p);
    // 云端作品：不自动下载缩略图（366 条会拖垮首屏），点开才拉取并缓存到本地
    const thumb = p.cloud_only
      ? `<div class="ph"><div class="phico">${m.kind==="video"?"☁ ▶":"☁"}</div><div class="phtxt">云端 · 点击加载</div></div>`
      : (m.kind==="video"
        ? `<video src="${m.url}" muted preload="metadata" playsinline></video><div class="play"><span>▶</span></div>`
        : `<img src="${m.url}" loading="lazy" alt=""/>`);
    const badge = p.kind==="live"?`<div class="badge live">实况图</div>`
               : p.kind==="video"?`<div class="badge video">视频</div>`
               : `<div class="badge image">图文</div>`;
    const cbadge = p.cloud_only?`<div class="badge cloud">☁ 云端</div>`:"";
    const more = p.media.length>1?`<div class="more">${p.media.length} 项</div>`:"";
    c.innerHTML=`<div class="thumb">${thumb}${badge}${cbadge}${more}</div>
      <div class="cap"><div class="d">${esc(p.desc)}</div><div class="t">${esc(p.account)} · ${esc(p.pub||"")}</div></div>`;
    c.onclick=()=>openModal(p);
    grid.appendChild(c);
  });
}

/* 灯箱 */
let CUR=null, IDX=0;
function openModal(p){ CUR=p; IDX=0; $("#modal").classList.add("on"); renderModal(); }
function renderModal(){
  const p=CUR; if(!p) return;
  const stage=$("#mstage"); const m=p.media[IDX];
  const hint = p.cloud_only ? `<div class="loadhint" id="loadhint">☁ 正在从云端拉取到本地缓存…<br/>（已缓存的会秒开）</div>` : "";
  stage.innerHTML = m.kind==="video"
    ? `<div class="mnav l" onclick="nav(-1)">‹</div><div class="mnav r" onclick="nav(1)">›</div><video src="${m.url}" controls autoplay playsinline style="max-height:68vh"></video>${hint}`
    : `<div class="mnav l" onclick="nav(-1)">‹</div><div class="mnav r" onclick="nav(1)">›</div><img src="${m.url}"/>${hint}`;
  const mediaEl = stage.querySelector("video,img");
  if(mediaEl && p.cloud_only){
    const hide=()=>{ const h=document.getElementById("loadhint"); if(h) h.remove(); };
    mediaEl.addEventListener("loadeddata", hide);
    mediaEl.addEventListener("load", hide);
    mediaEl.addEventListener("error", ()=>{ const h=document.getElementById("loadhint");
      if(h) h.innerHTML="☁ 拉取失败<br/>（可能文件过大或网络不稳，点下方「下载」直连试试）"; });
  }
  const strip=$("#mstrip"); strip.innerHTML="";
  if(p.cloud_only){
    // 云端作品可能几十张图，不预取缩略图；只给序号导航
    strip.innerHTML=`<div style="padding:4px 8px;color:#8b93a7;font-size:12px">
      ☁ 云端作品 ${IDX+1}/${p.media.length} · 首次打开会下载到本地缓存，之后秒开${p.media.length>1?"（用 ← → 切换）":""}</div>`;
  } else {
    p.media.forEach((mm,i)=>{
      const e=document.createElement(mm.kind==="video"?"video":"img");
      e.src=mm.url; e.className=i===IDX?"on":""; e.onclick=()=>{IDX=i;renderModal();};
      strip.appendChild(e);
    });
  }
  $("#minfo").innerHTML=`<div class="desc">${esc(p.desc)}</div>
    <div class="meta"><span>${esc(p.account)}</span><span>${esc(p.pub||"")}</span>
    ${p.link?`<a href="${esc(p.link)}" target="_blank">抖音原作品 ↗</a>`:""}
    <a href="${esc(p.cloud_only&&m.remote?m.remote:m.url)}" target="_blank" download>下载</a></div>`;
}
function nav(d){ if(!CUR) return; IDX=(IDX+d+CUR.media.length)%CUR.media.length; renderModal(); }
function closeModal(){ $("#modal").classList.remove("on"); CUR=null; }
$("#modal").addEventListener("click",e=>{ if(e.target.id==="modal") closeModal(); });
document.addEventListener("keydown",e=>{ if(e.key==="Escape")closeModal(); if(e.key==="ArrowLeft")nav(-1); if(e.key==="ArrowRight")nav(1); });

$("#search").addEventListener("input",e=>{ FILTER.q=e.target.value.trim(); renderGrid(); });
$("#srcs").addEventListener("click",e=>{
  const t=e.target.closest(".src"); if(!t) return;
  FILTER.src=t.dataset.src;
  document.querySelectorAll("#srcs .src").forEach(s=>s.classList.toggle("on",s===t));
  renderGrid();
});
$("#syncBtn").addEventListener("click",async()=>{
  const b=$("#syncBtn"); b.disabled=true; b.textContent="同步中…";
  try{ await fetch("/api/trigger",{method:"POST"}); }catch(e){}
  setTimeout(()=>{ b.disabled=false; b.textContent="↻ 立即同步"; loadStatus(); },1500);
});
$("#cloudBtn").addEventListener("click",async()=>{
  const b=$("#cloudBtn"); b.disabled=true; b.textContent="上传中…";
  try{ await fetch("/api/cloud/sync",{method:"POST"}); }catch(e){}
  setTimeout(()=>{ b.disabled=false; b.textContent="☁ 上传云端"; loadCloud(); },1500);
});
$("#rebuildBtn").addEventListener("click",async()=>{
  const b=$("#rebuildBtn"); b.disabled=true; b.textContent="触发中…";
  try{ const r=await fetch("/api/cloud/rebuild",{method:"POST"}); const j=await r.json();
    if(!j.ok) alert("触发失败: "+(j.reason||"")); }catch(e){}
  setTimeout(()=>{ b.disabled=false; b.textContent="🔄 刷新云端画廊"; loadCloud(); },1500);
});

async function loadCloud(){
  try{
    const r=await fetch("/api/cloud/status"); const c=await r.json();
    const el=$("#cloudMeta");
    if(c.enabled){
      const up=c.last_upload||{};
      const perm=c.permission||{};
      if(perm.ok===false){
        el.innerHTML=`☁ 已连仓库 ${c.repo||""}，但<span style="color:#ff8a80">令牌缺少写权限</span>：${perm.reason||""}`;
        return;
      }
      let s=`☁ 云端直传已开启 · 仓库 ${c.repo||""} · 本次上传 ${up.uploaded||0} · 云端共 ${up.total_in_cloud||0} 个`;
      if(c.last_push) s+= c.last_push.ok?" · 索引已推送 ✓":" · 索引未推送";
      el.textContent=s;
    } else {
      el.textContent="☁ 云端未配置（在 .env 填 GITHUB_TOKEN 即自动开启本地→云端直传）";
    }
  }catch(e){}
}

load();
setInterval(loadStatus, 8000);
setInterval(loadCloud, 12000);
setInterval(()=>{ if(!CUR) load(); }, 30000);  // 后台静默刷新索引(灯箱打开时不打断)
</script>
</body>
</html>"""


def _trigger_cloud():
    """后台触发一次 本地→云端 直传 + 推索引。"""
    if not CLOUD_SYNC:
        return {"enabled": False, "reason": "未配置 GITHUB_TOKEN，无法直传云端"}
    threading.Thread(target=lambda: cloud_sync.cloud_sync_once(
        None, max_per_run=CLOUD_MAX_PER_RUN), daemon=True).start()
    return {"triggered": True}


def _cloud_status():
    """云端状态：以「配置是否齐全」为准实时判断，再叠加最近一次同步结果与写权限。"""
    if not CLOUD_SYNC:
        return {"enabled": False, "reason": "未配置 GITHUB_TOKEN"}
    st = dict(cloud_sync.load_status() or {})
    st["enabled"] = True
    repo = cloud_sync.ensure_repo()[0]
    st["repo"] = repo or st.get("repo", "")
    st["permission"] = cloud_sync.check_write_permission()
    return st


class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinLocalServer/1.0"

    def log_message(self, *a):  # 静默默认访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._html(APP_HTML)
        if p == "/api/index":
            return self._json(build_index())
        if p == "/api/status":
            return self._json({
                "monitor_running": monitor.running,
                "last_run": monitor.last_run,
                "last_status": monitor.last_status,
                "next_run": (monitor.next_run and time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(monitor.next_run))) or "",
                "public_url": monitor.public_url,
                "lan_url": f"http://{_lan_ip()}:{PORT}",
                "cache_dir": str(CACHE_DIR),
                "cloud_sync": CLOUD_SYNC,
                "cloud": _cloud_status() if CLOUD_SYNC else {"enabled": False, "reason": "未配置 GITHUB_TOKEN"},
            })
        if p == "/api/log":
            return self._json({"log": monitor.log[-200:]})
        if p == "/api/cloud/status":
            return self._json(_cloud_status())
        if p == "/api/cloud/sync":
            return self._json(_trigger_cloud())
        if p == "/api/cloud/rebuild":
            return self._json(cloud_sync.trigger_pages_rebuild())
        if p == "/api/trigger":
            ok = monitor.trigger()
            return self._json({"triggered": ok, "running": monitor.running})
        if p.startswith("/media/"):
            return self.serve_media(p[len("/media/"):])
        if p == "/cloud":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            u = (qs.get("u") or [""])[0]
            if not u:
                return self.send_error(400, "Bad Request")
            return self.serve_cloud(u)
        self.send_error(404, "Not Found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/trigger":
            ok = monitor.trigger()
            return self._json({"triggered": ok, "running": monitor.running})
        if u.path == "/api/cloud/sync":
            return self._json(_trigger_cloud())
        if u.path == "/api/cloud/rebuild":
            return self._json(cloud_sync.trigger_pages_rebuild())
        self.send_error(404, "Not Found")

    def serve_cloud(self, u):
        """云端媒体代理：先下载到本地缓存，再用正确 MIME / 支持 Range 输出。

        GitHub Releases 附件下载一律是 application/octet-stream + attachment，
        浏览器 <video> 可能拒播；走本地代理后由本机按扩展名发 MIME，实测可拖动。
        """
        try:
            pr = urllib.parse.urlparse(u)
        except Exception:
            return self.send_error(400, "Bad Request")
        if pr.scheme not in ("http", "https") or pr.hostname not in _CLOUD_ALLOW_HOSTS:
            return self.send_error(403, "Forbidden")
        fp = fetch_cloud_to_cache(u)
        if not fp:
            # 下载失败/超大文件：退化为 302 直连云端，浏览器自己处理
            self.send_response(302)
            self.send_header("Location", u)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rel = fp.relative_to(CACHE_DIR.resolve()).as_posix()
        return self.serve_media(rel)

    def serve_media(self, rel):
        # 防目录穿越
        fp = (CACHE_DIR / urllib.parse.unquote(rel)).resolve()
        root = CACHE_DIR.resolve()
        try:
            fp.relative_to(root)
        except ValueError:
            return self.send_error(403, "Forbidden")
        if not fp.exists() or not fp.is_file():
            return self.send_error(404, "Not Found")
        size = fp.stat().st_size
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)$", rng.strip())
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                if start <= end < size:
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    with open(fp, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                    return
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        with open(fp, "rb") as f:
            shutil.copyfileobj(f, self.wfile)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    lan = _lan_ip()
    plog("抖音本地服务器已启动")
    plog(f"  本机:      http://localhost:{PORT}")
    plog(f"  同WiFi手机: http://{lan}:{PORT}")
    plog(f"  缓存目录:  {CACHE_DIR}")
    plog(f"  监控间隔:  {INTERVAL} 分钟（后台自动）")
    if CLOUD_SYNC:
        plog("  云端直传:  已开启（监控下载后自动上传 GitHub Releases + 推送 gallery.json 索引）")
    else:
        plog("  云端直传:  未开启（在 .env 填 GITHUB_TOKEN 即自动开启）")
    if EXPOSE_PUBLIC:
        plog("  公网隧道:  正在尝试开通（首次需联网下载 cloudflared）…")
    plog("  按 Ctrl+C 停止")
    # 后台监控线程
    threading.Thread(target=monitor.loop, daemon=True).start()
    # 公网隧道（best-effort）
    if EXPOSE_PUBLIC:
        threading.Thread(target=try_expose, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        monitor._stop = True
        srv.shutdown()


if __name__ == "__main__":
    main()
