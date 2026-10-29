#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""catbox.moe 匿名文件床上传（零配置，无需任何密钥）。

把下载好的作品传到 catbox.moe, 返回永久直链(https://files.catbox.moe/xxx.mp4),
可直接放进 <video src> 在线播放。

特点
----
  - 匿名上传: 不需要账号、不需要 API Key、不需要任何环境变量。
  - 永久保留: 匿名上传的文件不会自动过期(官方说明)。
  - 单文件上限 200MB(超出会返回错误, 本脚本会保留本地备份并跳过该作品)。
  - 注意: catbox 服务器在国外, 国内访问可能偏慢/不稳; 官方不建议作为大规模视频流 CDN。
    若国内播放体验差, 可改用腾讯云 COS 等国内对象存储(另行接入)。

环境变量
--------
  STORAGE_BACKEND  设为 catbox(默认) 即启用本后端; 设为 local 则关闭上传退回本地。
"""
import os
from pathlib import Path

API = "https://catbox.moe/user/api.php"


def reload() -> None:
    """兼容接口: catbox 无需读取环境变量, 这里为空操作。"""
    return


def enabled() -> bool:
    """catbox 是否启用。除非显式设为 local, 否则默认启用。"""
    return os.environ.get("STORAGE_BACKEND", "catbox").strip().lower() != "local"


def public_url(key: str) -> str:
    """catbox 的上传结果本身就是完整直链, key 即 URL。"""
    return key


def upload(local_path: Path, key: str = None) -> str:
    """上传单个文件, 成功返回永久直链, 失败返回 ''。

    catbox 接口返回纯文本直链(如 https://files.catbox.moe/abc123.mp4)。
    单文件上限 200MB, 超出会返回错误文本而非直链。
    """
    import requests
    try:
        with open(local_path, "rb") as f:
            r = requests.post(
                API,
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (local_path.name, f)},
                timeout=180,
            )
        text = r.text.strip()
        if r.status_code == 200 and text.startswith("https://"):
            print(f"    [catbox] 上传成功: {local_path.name} -> {text}")
            return text
        print(f"    !! catbox 上传失败 {local_path.name}: HTTP {r.status_code} | {text[:200]}")
        return ""
    except Exception as e:
        print(f"    !! catbox 上传异常 {local_path.name}: {e}")
        return ""
