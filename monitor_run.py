#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音账号监控(无头版): 增量发现新作品 -> 下载 -> 推送微信

设计说明
--------
DouK-Downloader 的"终端交互模式"依赖剪贴板监听(同步阻塞调用),
在无桌面/无 TTY 的环境(沙箱、CI、定时任务)里会卡死整个事件循环,
且其剪贴板监听任务不会随退出指令结束, 进程无法干净退出。
因此本脚本不调用 DouK 的终端 UI, 而是直接复用 DouK 的反爬签名模块
(src/encrypt/douyin_params.DouYinParams) 生成 a_bogus / web_sign,
自行完成: 拉取作品列表 -> 按类型取直链 -> 下载 -> 增量去重 -> 推送。
目标与原始方案完全一致: 监控指定账号, 发布新作品自动下载并推送到微信。

读取的环境变量(.env 或 GitHub Actions Secrets):
  DOUYIN_COOKIE       抖音 Cookie(务必用小号)               [必填]
  MONITOR_URLS        监控的账号主页链接, 英文逗号分隔        [必填]
  SERVERCHAN_SENDKEY  Server酱 SendKey(推送微信用)         [可选]
  DOWNLOAD_DIR        下载根目录, 默认 ./downloads          [可选]
  MAX_PAGES           每次最多翻几页(控制耗时), 默认 5        [可选]
  MAX_NEW_PER_RUN     每轮最多处理多少条新作品, 默认 50       [可选]
  TIME_BUDGET_MIN     每轮时间预算(分钟), 到点主动收尾, 默认 45  [可选]
  ACCOUNTS_URL        在线电子表格CSV地址(发布为CSV的Google表格/腾讯文档), 运行时拉取并合并 [可选]
  STORAGE_BACKEND     存储后端: gh_release(默认)/r2/local          [可选]
"""
import csv
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests
from curl_cffi.requests import Session

ROOT = Path(__file__).parent

# 复用 DouK 的反爬签名模块(必须让 `from src...` 可导入)
sys.path.insert(0, str(ROOT / "douk_src"))
from src.encrypt.douyin_params import DouYinParams, USERAGENT  # noqa: E402

# 远端存储后端: 默认 gh_release(上传到本仓库的 GitHub Release 附件,
# 只需 Actions 自带的 GITHUB_TOKEN, 无需任何外部账号/密钥);
# 设 STORAGE_BACKEND=r2 可切回 Cloudflare R2(需配置 R2_* 环境变量)。
sys.path.insert(0, str(ROOT))
_BACKEND = os.environ.get("STORAGE_BACKEND", "gh_release").strip().lower()
# KEEP_LOCAL=1：本地服务器模式下，上传云端后**不允许删除本地副本**（D:\gitdy 是长期缓存）
KEEP_LOCAL = str(os.environ.get("KEEP_LOCAL", "")).strip().lower() in ("1", "true", "yes", "on")


class _LocalOnlyStore:
    """STORAGE_BACKEND=local 时使用的空存储：不联网、不上传、不删本地。

    注意：不能只靠「没配 token」来判定本地模式——本地服务器的 .env 里通常带着
    PAT（用于本地→云端直传），子进程一旦继承它，就会走 gh_release 分支把本地
    缓存上传后删掉。所以这里显式禁用。
    """

    def reload(self):
        return None

    def enabled(self):
        return False

    def upload(self, *a, **k):
        return ""

    def peek(self, *a, **k):
        return ""

    def self_heal_mime(self, *a, **k):
        return 0


if _BACKEND == "local":
    store = _LocalOnlyStore()
elif _BACKEND == "r2":
    import r2_store as store  # noqa: E402
else:
    import gh_release_store as store  # noqa: E402


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def _load_local_env():
    """本地运行时从 .env 读取配置（GitHub Actions 无此文件则自动跳过）。"""
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

# ---------------------------------------------------------------------------
# 日志: 同步输出到控制台和 run_log.txt (定时任务静默运行时可查历史)
# ---------------------------------------------------------------------------
class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:
                pass

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


_LOG = open(ROOT / "run_log.txt", "a", encoding="utf-8", errors="ignore")
_LOG.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 运行开始 =====\n")
_LOG.flush()
sys.stdout = _Tee(sys.stdout, _LOG)
sys.stderr = _Tee(sys.stderr, _LOG)

# 清理代理环境变量: 部分代理会拦截 douyin.com(502), 直连最稳。
# 用户的真实机器通常无此代理, 下面这行是空操作, 不影响。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", ROOT / "downloads"))
# 每个监控账号一个独立状态文件 state/<sec_uid>.json（已知作品 known + 已归档直链 works），
# 不再把所有账号全部堆进同一个 state.json，便于按账号管理/排查/单独重置。
STATE_DIR = ROOT / "state"
# 可编程账号表格：在仓库里直接编辑 accounts.csv 即可增删监控对象
ACCOUNTS_CSV = ROOT / "accounts.csv"
SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
COOKIE = os.environ.get("DOUYIN_COOKIE", "").strip()
# 旧的环境变量账号列表，仍作为补充来源（平滑过渡到 accounts.csv）
URLS_ENV = [u.strip() for u in os.environ.get("MONITOR_URLS", "").split(",") if u.strip()]
# 可选: 在线电子表格(发布为 CSV 的 Google 表格 / 腾讯文档导出)地址,
# workflow 运行时自动拉取并合并, 在电子表格界面增删监控对象更方便。
ACCOUNTS_URL = (os.environ.get("ACCOUNTS_URL", "") or "").strip()
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5").strip() or "5")
# 每轮最多下载多少条"新作品", 防止首次接入大量账号时一次性全量下载导致
# 超时 / 产物超限 / 缓存爆掉。未处理完的会留到下一轮继续(渐进式补齐)。
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "50").strip() or "50")
# 本轮时间预算(分钟): 到点就主动收尾并落盘, 让后续步骤(生成画廊/部署/推送)
# 正常执行。避免像"被 job 超时硬杀"那样导致本轮进度全部丢失。
# 默认 45 分钟, 留出余量给缓存上传与 Pages 部署。
TIME_BUDGET_MIN = float(os.environ.get("TIME_BUDGET_MIN", "45").strip() or "45")
# 单个媒体文件的下载总时长上限(秒)。慢速大文件会拖垮整轮时间预算,
# 超时即放弃该文件(下轮再试), 保证本轮能按时收尾并落盘。
DL_TIMEOUT = float(os.environ.get("DL_TIMEOUT", "240").strip() or "240")
_RUN_START = time.time()


def budget_left() -> bool:
    """本轮还剩时间预算就返回 True。"""
    return (time.time() - _RUN_START) < TIME_BUDGET_MIN * 60

PROXIES = {"http": None, "https": None}  # 直连
DOUK = DouYinParams()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def parse_cookie(raw: str) -> dict:
    raw = raw.strip()
    data = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def extract_sec_user_id(url: str) -> str:
    """从 https://www.douyin.com/user/MS4wLjAB... 取 sec_user_id。"""
    m = re.search(r"/user/([^/?#]+)", url)
    return m.group(1) if m else url.strip()


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip()
    return name[:40] or "post"


def _build_params(sec_user_id: str, cursor: int, cookie: dict) -> dict:
    base = {
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "update_version_code": "170400", "pc_client_type": "1", "pc_libra_divert": "Mac",
        "support_h265": "1", "support_dash": "1", "version_code": "290100", "version_name": "29.1.0",
        "cookie_enabled": "true", "screen_width": "1536", "screen_height": "864",
        "browser_language": "zh-CN", "browser_platform": "MacIntel", "browser_name": "Chrome",
        "browser_version": "146.0.0.0", "browser_online": "true", "engine_name": "Blink",
        "engine_version": "146.0.0.0", "os_name": "Mac OS", "os_version": "10.15.7",
        "cpu_core_num": "16", "device_memory": "8", "platform": "PC", "downlink": "10",
        "effective_type": "4g", "round_trip_time": "200",
        "uifid": cookie.get("UIFID", ""), "msToken": "",
    }
    base.update({
        "sec_user_id": sec_user_id, "max_cursor": str(cursor), "locate_query": "false",
        "show_live_replay_strategy": "1", "need_time_list": "1", "time_list_query": "0",
        "whale_cut_token": "", "cut_version": "1", "count": "20",
        "publish_video_strategy_type": "2", "from_user_page": "1",
    })
    return base


def fetch_post_list(session: Session, cookie: dict, sec_user_id: str) -> list:
    """翻页拉取账号作品, 最多 MAX_PAGES 页。返回 aweme 列表。"""
    out = []
    cursor = 0
    api = "https://www.douyin.com/aweme/v1/web/aweme/post/"
    headers = {
        "User-Agent": USERAGENT,
        "Referer": "https://www.douyin.com/?recommend=1",
        "Accept": "application/json, text/plain, */*",
        "uifid": cookie.get("UIFID", ""),
    }
    for _ in range(MAX_PAGES):
        params = _build_params(sec_user_id, cursor, cookie)
        signed = DOUK.sign_url(url=api, query=params, user_agent=USERAGENT, ms_token="")
        try:
            r = session.get(api + "?" + signed, headers=headers, cookies=cookie,
                            proxies=PROXIES, timeout=25)
            j = r.json()
        except Exception as e:
            print(f"!! 拉取作品失败: {e}")
            break
        if j.get("status_code") != 0:
            print(f"!! 接口返回异常 status_code={j.get('status_code')}, msg={j.get('status_msg')}")
            break
        batch = j.get("aweme_list") or []
        if not batch:
            break
        out.extend(batch)
        if not j.get("has_more"):
            break
        cursor = j.get("max_cursor", 0)
    return out


def select_media(aweme: dict):
    """返回 [(url, ext, kind), ...]。图文走 images 图集; 视频走 play/download 直链。

    说明: 抖音视频直链(douyinvod.com)通常不带 .mp4 后缀, 不能按后缀判断;
    而图文作品的 video.play_addr 存的是背景音乐(.mp3), 因此 images 优先。
    """
    # 1) 图文: images 字段存在则取图集
    imgs = aweme.get("images") or []
    if imgs:
        urls = []
        for img in imgs:
            ul = img.get("url_list") or []
            # Live 图 / 动态照片: 动态视频藏在 image.video 里
            #   (images[].video.play_addr.url_list)。视频自带封面帧, 因此
            #   该图只存视频即可, 不重复存静态封面(否则画廊封面与动态重复)。
            iv = (img.get("video") or {})
            ivnode = iv.get("play_addr") or iv.get("download_addr") or {}
            ivul = ivnode.get("url_list") or []
            if ivul:
                vu = ivul[-1]
                vext = "mov" if ".mov" in vu.lower() else "mp4"
                urls.append((vu, vext, "video"))
            elif ul:
                urls.append((ul[-1], "jpg", "image"))  # 取最高清的一张
        if urls:
            return urls
    # 2) 视频: 无 images 时 play/download_addr 即视频直链(可能无后缀)
    v = aweme.get("video") or {}
    for key in ("download_addr", "play_addr"):
        node = v.get(key) or {}
        for u in node.get("url_list", []):
            if u and not u.lower().endswith((".mp3", ".m4a", ".aac")):
                return [(u, "mp4", "video")]
    return []


def cover_url(aweme: dict) -> str:
    """取作品封面图直链（用于微信内在线预览，best-effort）。

    抖音官方接口里封面通常在 video.cover / video.origin_cover / video.dynamic_cover
    的 url_list 中。部分封面地址带签名且可能过期，加载不出来时以作品链接为准。
    """
    v = aweme.get("video") or {}
    for key in ("cover", "origin_cover", "dynamic_cover"):
        node = v.get(key) or {}
        ul = node.get("url_list") or []
        if ul:
            return ul[0]
    return ""


def download_file(session: Session, url: str, cookie: dict, path: Path) -> bool:
    """下载单个媒体。socket 超时之外再加单文件总时长上限。

    只靠 socket 超时挡不住"慢速但一直有数据"的连接(大视频可能拖几十分钟),
    那样会把本轮时间预算耗光、来不及落盘。这里超时即放弃并删除半截文件。
    """
    started = time.time()
    try:
        r = session.get(url, headers={
            "User-Agent": USERAGENT,
            "Referer": "https://www.douyin.com/",
        }, cookies=cookie, proxies=PROXIES, timeout=90, stream=True)
        if r.status_code != 200:
            print(f"!! 下载失败 {path.name}: HTTP {r.status_code}")
            return False
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if time.time() - started > DL_TIMEOUT:
                    print(f"!! 下载超时(>{DL_TIMEOUT:.0f}s) 放弃: {path.name}")
                    break
                if chunk:
                    f.write(chunk)
        if path.stat().st_size <= 0:
            path.unlink(missing_ok=True)
            return False
        return True
    except Exception as e:
        print(f"!! 下载异常 {path.name}: {e}")
        return False


def push(title: str, msg: str) -> None:
    if not SENDKEY:
        print(f"==> [未配置推送] {title}\n{msg[:300]}")
        return
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{SENDKEY}.send",
                          data={"title": title, "desp": msg}, timeout=15)
        print(f"==> 推送结果: {r.status_code}")
    except Exception as e:
        print(f"==> 推送失败: {e}")


def state_path(sec: str) -> Path:
    """账号状态文件路径（sec_uid 可能含特殊字符，做一遍安全化）。"""
    return STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', sec)[:80]}.json"


def load_account_state(sec: str) -> dict:
    """读取某个账号的状态；不存在则返回空骨架。"""
    p = state_path(sec)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"sec_uid": sec, "nickname": "", "known": [], "works": {}}


def save_account_state(sec: str, st: dict) -> None:
    """把某个账号的已知作品与已归档直链写回它自己的文件。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_path(sec).write_text(
            json.dumps(st, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        print(f"!! 保存账号状态失败 {sec}: {e}")


def _parse_account_rows(reader, rows, disabled) -> None:
    """解析 CSV 行迭代器(每行为字段列表), 追加到 rows / disabled。

    列：sec_uid,url,nickname,enabled,remark
      - sec_uid 为空时从 url 提取
      - enabled 为 0/false/no/off 视为停用
    """
    for parts in reader:
        # 整行都空才跳过：sec_uid 列允许留空，只填 url 同样有效
        if not parts or not any(p.strip() for p in parts):
            continue
        first = parts[0].strip()
        if first.startswith("#") or first.lower() == "sec_uid":
            continue  # 注释行 / 表头
        sec_uid = first
        url = parts[1].strip() if len(parts) > 1 else ""
        nick = parts[2].strip() if len(parts) > 2 else ""
        enabled = parts[3].strip().lower() if len(parts) > 3 else "1"
        # 跳过示例/占位行（如 REPLACE_WITH_DOUYIN_URL），否则会拿假链接去抓
        if url and (not url.lower().startswith(("http://", "https://"))
                    or "replace_with" in url.lower()):
            continue
        sec = sec_uid or (extract_sec_user_id(url) if url else "")
        if not sec:
            continue
        if enabled in ("0", "false", "no", "off"):
            disabled.add(sec)
            continue
        rows.append((sec, nick))


def load_accounts() -> list:
    """监控账号来源(三者合并去重):
      1) accounts.csv  (仓库内可编程表格)
      2) ACCOUNTS_URL  (发布为 CSV 的在线电子表格, 可选)
      3) MONITOR_URLS  (历史环境变量来源, 平滑过渡)
    返回 [(sec_uid, nickname), ...]
    """
    rows, disabled = [], set()

    # 1) 仓库内 accounts.csv
    if ACCOUNTS_CSV.exists():
        try:
            with open(ACCOUNTS_CSV, "r", encoding="utf-8-sig", newline="") as f:
                _parse_account_rows(csv.reader(f), rows, disabled)
        except Exception as e:
            print(f"!! 读取 accounts.csv 失败: {e}")

    # 2) 在线电子表格(发布为 CSV 的 Google 表格 / 腾讯文档导出)
    if ACCOUNTS_URL:
        try:
            print("==> 拉取在线账号表 ACCOUNTS_URL ...")
            r = requests.get(ACCOUNTS_URL, timeout=30,
                             headers={"User-Agent": "douyin-monitor"})
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
            _parse_account_rows(csv.reader(io.StringIO(text)), rows, disabled)
            print("    在线账号表已合并")
        except Exception as e:
            print(f"!! 拉取 ACCOUNTS_URL 失败(已忽略, 继续使用其它来源): {e}")

    # 3) 历史环境变量来源(平滑过渡), 但 csv / 在线表里显式停用的除外
    for u in URLS_ENV:
        sec = extract_sec_user_id(u)
        if sec and sec not in disabled:
            rows.append((sec, ""))

    seen, out = set(), []
    for sec, nick in rows:
        if sec in seen:
            continue
        seen.add(sec)
        out.append((sec, nick))
    return out


# ---------------------------------------------------------------------------
# 作品归档: 每个作品一个文件夹(发布时间+文案命名), 文案存为 文案.txt
# ---------------------------------------------------------------------------
def work_folder(aweme: dict, sec: str) -> tuple:
    """返回 (作品文件夹 Path, 昵称, 原始文案, 发布时间文本)。
    文件夹命名: downloads/<账号昵称>/<发布时间>_<文案前30字>/
    """
    nick = _sanitize((aweme.get("author") or {}).get("nickname", sec[:8]))
    raw_desc = (aweme.get("desc") or "").strip()
    desc = _sanitize(raw_desc) if raw_desc else ""
    ct = aweme.get("create_time") or 0
    if ct:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(ct))
        pub = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ct))
    else:  # 无发布时间时用下载时间兜底
        ts = time.strftime("%Y%m%d_%H%M%S")
        pub = ts
    name = f"{ts}_{desc[:30]}".rstrip(" ._") if desc else ts
    # Windows 不允许文件夹名以空格/点结尾, 兜底再保险一次
    name = name.rstrip(" ._") or ts
    return DOWNLOAD_DIR / nick / name, nick, raw_desc, pub


def build_caption(aweme: dict, pub: str, raw_desc: str, kind: str) -> str:
    """生成作品文案文本（写进 文案.txt / 画廊展示共用）。"""
    st = aweme.get("statistics") or {}
    lines = [
        f"发布时间: {pub}",
        f"作品类型: {'视频' if kind == 'video' else '图文'}",
        "",
        "文案:",
        raw_desc or "(无文案)",
        "",
        f"点赞 {st.get('digg_count', '?')} | 评论 {st.get('comment_count', '?')} | "
        f"收藏 {st.get('collect_count', '?')} | 分享 {st.get('share_count', '?')}",
        f"作品链接: https://www.douyin.com/video/{aweme.get('aweme_id', '')}",
    ]
    return "\n".join(lines)


def write_caption_txt(aweme: dict, folder: Path, pub: str, raw_desc: str,
                      kind: str) -> None:
    """把文案及作品信息保存到 文案.txt。"""
    try:
        (folder / "文案.txt").write_text(
            build_caption(aweme, pub, raw_desc, kind), "utf-8")
    except Exception as e:
        print(f"    !! 写文案失败: {e}")


def download_work(session: Session, cookie: dict, aweme: dict, sec: str) -> list:
    """下载单个作品: 每个作品独立文件夹, 文案保存为 文案.txt。返回成功落盘的文件列表。"""
    media = select_media(aweme)
    if not media:
        return []
    folder, _nick, raw_desc, pub = work_folder(aweme, sec)
    folder.mkdir(parents=True, exist_ok=True)
    kind = media[0][2]
    saved = []
    for i, (u, ext, kind_i) in enumerate(media):
        fname = _media_filename(i, media)
        path = folder / fname
        if download_file(session, u, cookie, path):
            saved.append(path)
            print(f"    [新] {kind_i} 已下载: {path.parent.name}/{path.name} "
                  f"({path.stat().st_size // 1024} KB)")
    if saved:
        write_caption_txt(aweme, folder, pub, raw_desc, kind)
    else:
        # 一个文件都没下成功, 清掉空文件夹
        try:
            if not any(folder.iterdir()):
                folder.rmdir()
        except Exception:
            pass
    return saved


def _download_subset(session: Session, cookie: dict, aweme: dict, sec: str,
                    media: list, missing_idx: list) -> list:
    """只下载 media 中指定下标(缺失)的条目, 返回落盘文件列表。

    用于"补抓": 已 known 作品云端缺 live 动态视频等媒体时, 仅补下载
    缺失部分, 不重复下载已归档的图/视频。
    """
    if not missing_idx:
        return []
    folder, _nick, _raw, _pub = work_folder(aweme, sec)
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for i in missing_idx:
        u, ext, kind_i = media[i]
        path = folder / _media_filename(i, media)
        if download_file(session, u, cookie, path):
            saved.append(path)
            print(f"    [补] {kind_i} 已下载: {path.parent.name}/{path.name} "
                  f"({path.stat().st_size // 1024} KB)")
    return saved


def _media_filename(i: int, media: list) -> str:
    """与 download_work / predicted_keys 一致的本地文件名规则。

    多图作品: 01.jpg 02.jpg ...; 单文件: 视频.mp4 / 01.jpg。
    抽成共用函数, 保证"下载命名"与"补抓预测 key"永远对齐。
    """
    _u, ext, kind_i = media[i]
    if len(media) > 1:                 # 图文 / 多图
        return f"{i + 1:02d}.{ext}"
    return f"视频.{ext}" if kind_i == "video" else f"01.{ext}"


def predicted_keys(aweme: dict, sec: str, media: list) -> list:
    """不下载的前提下，预测该作品上传后会用到的 key 列表。

    与 download_work 的命名规则保持一致（同一 work_folder + 同名文件），
    因此预测命中就意味着该作品确实已归档过。
    """
    folder, _nick, _raw, _pub = work_folder(aweme, sec)
    rel = folder.relative_to(DOWNLOAD_DIR).as_posix()
    keys = []
    for i, _ in enumerate(media):
        keys.append(f"{rel}/{_media_filename(i, media)}")
    return keys


def try_reuse_archived(aweme: dict, sec: str, media: list) -> list:
    """该作品若此前已归档过，直接返回其直链列表（无需重复下载）。

    回灌历史作品时能省掉绝大部分下载耗时；未命中返回 []。
    """
    peek = getattr(store, "peek", None)
    if not peek:
        return []
    urls = []
    for k in predicted_keys(aweme, sec, media):
        u = peek(k)
        if not u:
            return []
        urls.append(u)
    out = []
    for u in urls:
        low = u.split("?")[0].lower()
        kind = "video" if low.endswith((".mp4", ".webm", ".mov")) else "image"
        out.append((kind, u))
    return out


# ---------------------------------------------------------------------------
# 云端归档: 上传成功后删除本地副本, 让 Actions 缓存永远保持极小
# ---------------------------------------------------------------------------
def archive_work(saved: list) -> list:
    """把作品文件上传到远端存储, 返回 [(type, public_url), ...]。

    全部文件上传成功后删除本地文件夹(含文案.txt)。这样 downloads/ 不会累积,
    Actions 缓存不会超限, 1188+ 条作品也能长期保存。
    任一项上传失败(如单文件超过 GitHub 2GB 上限)则保留本地备份并返回 []。
    """
    if not saved or not store.enabled():
        return []
    folder = saved[0].parent
    rel = folder.relative_to(DOWNLOAD_DIR).as_posix()
    uploaded, ok = [], True
    for p in sorted(saved):
        key = f"{rel}/{p.name}"
        url = store.upload(p, key)
        if url:
            kind = ("video" if p.suffix.lower() in (".mp4", ".webm", ".mov")
                    else "image")
            uploaded.append((kind, url))
        else:
            ok = False
    if uploaded and ok:
        if KEEP_LOCAL:
            # 本地服务器模式：D:\gitdy 是长期缓存，只上传不删本地
            print(f"    [store] 已上传(保留本地副本, KEEP_LOCAL=1): {rel}")
            return uploaded
        try:
            shutil.rmtree(folder)
            print(f"    [store] 已上传并清理本地: {rel}")
        except Exception as e:
            print(f"    !! 清理本地失败 {rel}: {e}")
        return uploaded
    print(f"    [store] 上传未全部成功, 保留本地备份: {rel}")
    return []


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    # .env 是在模块导入之后才载入的, 这里重新读取一次存储后端配置
    store.reload()
    store_on = store.enabled()
    backend_label = ("Cloudflare R2" if _BACKEND == "r2"
                     else "GitHub Releases（本仓库附件）" if store_on
                     else "本地目录（未启用云端存储）")
    print(f"==> 存储模式: {backend_label}")
    # 旧视频 MIME 修正: 把此前被存成 application/octet-stream 的视频以正确
    # video/mp4 重传回原 release(直链 URL 不变), 让画廊能内嵌播放。
    if store_on:
        try:
            n_fixed = store.self_heal_mime()
            print(f"==> 旧视频 MIME 修正: 本轮修正 {n_fixed} 个")
        except Exception as e:
            print(f"!! 旧视频 MIME 修正异常(已跳过, 不影响主流程): {e}")
    if not COOKIE:
        print("!! 未配置 DOUYIN_COOKIE")
        return 1
    accounts = load_accounts()
    if not accounts:
        print("!! 没有可监控的账号: accounts.csv 为空且未配置 MONITOR_URLS")
        return 1
    print(f"==> 待监控账号 {len(accounts)} 个(accounts.csv + MONITOR_URLS 合并)")

    cookie = parse_cookie(COOKIE)
    session = Session(impersonate="chrome")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    all_new = []  # (nick, desc, kind, n_files, post_url, cover)
    total_known = 0

    for sec, nick_csv in accounts:
        if len(all_new) >= MAX_NEW_PER_RUN:
            print(f"==> 已达每轮上限 {MAX_NEW_PER_RUN} 条, 剩余账号下轮继续")
            break
        if not budget_left():
            print(f"==> 已达本轮时间预算 {TIME_BUDGET_MIN} 分钟, 主动收尾(进度已落盘)")
            break
        print(f"==> 监控账号: {sec}")
        st = load_account_state(sec)
        known = set(st.get("known", []))
        # 自愈: 云端模式下, known 里没有对应归档记录的作品(此前上传失败或被
        # 中断遗留)其实并未真正入库, 本轮重新处理, 避免作品永久缺失。
        if store_on:
            works_now = st.get("works") or {}
            dropped = len(known) - len(known & set(works_now))
            if dropped:
                print(f"    发现 {dropped} 条未归档记录, 本轮重新处理")
            known = {k for k in known if k in works_now}

        def _flush():
            """每处理一条就落盘一次。

            回灌一次要下载+上传几十条作品, 耗时可能逼近 job 超时上限;
            若只在结尾保存, 中途被中断会导致本轮已完成的进度全部丢失、
            下轮又从头开始。这里增量保存, 保证进度不回退。
            """
            st["known"] = sorted(known)
            st["nickname"] = nick_csv or st.get("nickname", "")
            st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_account_state(sec, st)

        awemes = fetch_post_list(session, cookie, sec)
        print(f"    拉到作品 {len(awemes)} 条")
        # 按发布时间倒序, 保证先处理最新
        awemes.sort(key=lambda a: a.get("create_time", 0), reverse=True)
        for a in awemes:
            if len(all_new) >= MAX_NEW_PER_RUN:
                print(f"    [达到每轮上限 {MAX_NEW_PER_RUN}] 剩余作品下轮继续")
                break
            if not budget_left():
                print(f"    [达到时间预算 {TIME_BUDGET_MIN} 分钟] 剩余作品下轮继续")
                break
            aid = a.get("aweme_id")
            if not aid:
                continue
            media = select_media(a)
            if aid in known:
                # 补抓: 已 known 作品云端可能缺 live 动态视频等媒体
                if store_on:
                    rec = (st.get("works") or {}).get(aid)
                    if rec and media:
                        pk = predicted_keys(a, sec, media)
                        peek = getattr(store, "peek", None)
                        missing = [i for i, k in enumerate(pk)
                                   if not (peek(k) if peek else None)]
                        if missing:
                            print(f"    [补抓] {aid} 云端缺 {len(missing)} 个媒体"
                                  f"(如 live 动态), 补充下载")
                            saved = _download_subset(session, cookie, a, sec,
                                                    media, missing)
                            if saved:
                                new_items = archive_work(saved)
                                if new_items:
                                    exist = rec.setdefault("items", [])
                                    have = {(it.get("type"), it.get("url"))
                                            for it in exist}
                                    for t, u in new_items:
                                        if (t, u) not in have:
                                            exist.append({"type": t, "url": u})
                                    if any(it.get("type") == "video"
                                           for it in exist):
                                        rec["kind"] = "live"
                                    _flush()
                                    print(f"    [补抓完成] {aid} 现共 "
                                          f"{len(exist)} 个媒体")
                continue
            if not media:
                print(f"    [跳过] {aid} 无可下载媒体")
                known.add(aid)  # 确实没有可下媒体的, 不必反复重试
                _flush()
                continue
            # 此前已归档过的作品: 直接复用云端直链, 不再重复下载
            reused = try_reuse_archived(a, sec, media) if store_on else []
            if reused:
                _folder, nick, raw_desc, pub = work_folder(a, sec)
                ct = a.get("create_time") or 0
                st.setdefault("works", {})
                st["works"][aid] = {
                    "nick": nick,
                    "desc": _sanitize(a.get("desc", "") or "无标题"),
                    "pub": pub,
                    "ts": ct,
                    "kind": ("live" if any(m[2] == "video" for m in media)
                             and media[0][2] != "video" else media[0][2]),
                    "items": [{"type": t, "url": u} for t, u in reused],
                    "caption": build_caption(a, pub, raw_desc, media[0][2]),
                }
                print(f"    [已归档] {aid} 复用云端直链 {len(reused)} 个, 跳过下载")
                known.add(aid)
                _flush()
                continue
            saved = download_work(session, cookie, a, sec)
            if saved:
                nick = _sanitize((a.get("author") or {}).get("nickname", sec[:8]))
                desc = _sanitize(a.get("desc", "") or "无标题")
                post_url = f"https://www.douyin.com/video/{aid}" if aid else ""
                cov = cover_url(a)

                # 云端存储: 上传成功后删除本地副本, 直链写入该账号自己的状态文件
                archived = False
                if store_on:
                    ct = a.get("create_time") or 0
                    pub = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ct))
                           if ct else "")
                    capf = saved[0].parent / "文案.txt"
                    caption = (capf.read_text("utf-8", errors="ignore")
                               if capf.exists() else "")
                    items = archive_work(saved)
                    if items:
                        st.setdefault("works", {})
                        st["works"][aid] = {
                            "nick": nick,
                            "desc": desc or "无标题",
                            "pub": pub,
                            "ts": ct,
                            "kind": ("live" if any(m[2] == "video" for m in media)
                             and media[0][2] != "video" else media[0][2]),
                            "items": [{"type": t, "url": u} for t, u in items],
                            "caption": caption,
                        }
                        known.add(aid)  # 归档成功才算完成
                        archived = True
                    else:
                        # 上传失败: 不标记已知, 下轮重试(否则该作品会永久缺失)
                        print(f"    !! {aid} 归档失败, 保留待下轮重试")
                else:
                    known.add(aid)  # 本地模式: 下载成功即完成
                    archived = True
                if archived:
                    all_new.append((nick, desc, media[0][2], len(saved),
                                    post_url, cov))
            _flush()  # 每处理完一条立即落盘, 防止中断丢失进度
        _flush()
        total_known += len(known)
        print(f"    本账号累计已知 {len(known)} 条, 已归档 "
              f"{len(st.get('works', {}))} 条")

    # 汇总推送：仅文字摘要（不含抖音链接/封面，避免留下访问痕迹）
    if all_new:
        lines = "\n".join(
            f"- {nick}：{desc}（{'视频' if kind == 'video' else '图文'}）"
            for nick, desc, kind, n, _u, _c in all_new
        )
        push("抖音监控: 发现新作品", f"共 {len(all_new)} 条新作品，点开云端作品库可在线播放:\n\n{lines}")
    else:
        print("==> 无新作品")

    # 记录本次新作品数, 供 workflow 判断是否推送云端画廊链接
    try:
        (ROOT / "run_new_count.txt").write_text(str(len(all_new)), "utf-8")
    except Exception:
        pass

    print(f"==> 完成, 本轮新增 {len(all_new)} 条, 各账号累计已知 {total_known} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
