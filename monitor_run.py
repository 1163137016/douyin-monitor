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
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from curl_cffi.requests import Session

ROOT = Path(__file__).parent

# 复用 DouK 的反爬签名模块(必须让 `from src...` 可导入)
sys.path.insert(0, str(ROOT / "douk_src"))
from src.encrypt.douyin_params import DouYinParams, USERAGENT  # noqa: E402


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
STATE_FILE = ROOT / "state.json"
SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
COOKIE = os.environ.get("DOUYIN_COOKIE", "").strip()
URLS = [u.strip() for u in os.environ.get("MONITOR_URLS", "").split(",") if u.strip()]
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5").strip() or "5")

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
            if ul:
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
                if chunk:
                    f.write(chunk)
        return path.stat().st_size > 0
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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"known": []}


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


def write_caption_txt(aweme: dict, folder: Path, pub: str, raw_desc: str,
                      kind: str) -> None:
    """把文案及作品信息保存到 文案.txt。"""
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
    try:
        (folder / "文案.txt").write_text("\n".join(lines), "utf-8")
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
        if len(media) > 1:  # 图文: 01.jpg 02.jpg ...
            fname = f"{i + 1:02d}.{ext}"
        else:  # 单文件: 视频.mp4 / 01.jpg
            fname = f"视频.{ext}" if kind_i == "video" else f"01.{ext}"
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    if not COOKIE:
        print("!! 未配置 DOUYIN_COOKIE")
        return 1
    if not URLS:
        print("!! 未配置 MONITOR_URLS")
        return 1

    cookie = parse_cookie(COOKIE)
    session = Session(impersonate="chrome")
    state = load_state()
    known = set(state.get("known", []))
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    all_new = []  # (nick, desc, kind, n_files)
    seen_ids = set(known)

    for url in URLS:
        sec = extract_sec_user_id(url)
        print(f"==> 监控账号: {sec}")
        awemes = fetch_post_list(session, cookie, sec)
        print(f"    拉到作品 {len(awemes)} 条")
        # 按发布时间倒序, 保证先处理最新
        awemes.sort(key=lambda a: a.get("create_time", 0), reverse=True)
        for a in awemes:
            aid = a.get("aweme_id")
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            media = select_media(a)
            if not media:
                print(f"    [跳过] {aid} 无可下载媒体")
                continue
            saved = download_work(session, cookie, a, sec)
            if saved:
                nick = _sanitize((a.get("author") or {}).get("nickname", sec[:8]))
                desc = _sanitize(a.get("desc", "") or "无标题")
                aid = a.get("aweme_id")
                post_url = f"https://www.douyin.com/video/{aid}" if aid else ""
                cov = cover_url(a)
                all_new.append((nick, desc, media[0][2], len(saved), post_url, cov))

    # 汇总推送（含作品链接 + 封面，微信内可点开在线预览，无需下载）
    if all_new:
        blocks = []
        for nick, desc, kind, n, url, cov in all_new:
            blk = f"**{nick}**：{desc}\n"
            if cov:
                blk += f"![]({cov})\n"
            if url:
                blk += f"[在抖音打开预览]({url})\n"
            blk += f"类型: {'视频' if kind == 'video' else '图文'} · 文件数: {n}\n"
            blocks.append(blk)
        body = "\n---\n".join(blocks)
        push("抖音监控: 发现新作品", f"共 {len(all_new)} 条新作品:\n\n{body}")
    else:
        print("==> 无新作品")

    state["known"] = sorted(seen_ids)
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    print(f"==> 完成, 累计已知作品 {len(seen_ids)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
