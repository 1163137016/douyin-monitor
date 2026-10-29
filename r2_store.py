#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare R2 对象存储上传（S3 兼容 API）。

把下载好的作品传到 R2, 用公开域名直链播放, 从而摆脱 GitHub Pages 的容量上限。

环境变量（GitHub Actions Secrets 提供）:
  R2_ACCOUNT_ID         Cloudflare 账户 ID                    [必填]
  R2_ACCESS_KEY_ID      R2 API Token 的 Access Key ID          [必填]
  R2_SECRET_ACCESS_KEY  R2 API Token 的 Secret Access Key      [必填]
  R2_BUCKET             桶名                                   [必填]
  R2_PUBLIC_URL         公开访问域名(结尾不要带 /)             [必填]
                        例: https://pub-xxxxxxxx.r2.dev
                        或自定义域名 https://media.example.com
  R2_PREFIX             对象路径前缀, 默认 douyin              [可选]

说明: 全部变量缺失任一即视为"未启用 R2", 自动退回本地存储模式, 不会报错。
"""
import os
from pathlib import Path

ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
BUCKET = os.environ.get("R2_BUCKET", "").strip()
PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")
PREFIX = os.environ.get("R2_PREFIX", "douyin").strip().strip("/")

_client = None

# 常见后缀 -> Content-Type, 决定浏览器是"播放"还是"下载"
_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}


def reload() -> None:
    """重新读取环境变量。

    本地运行时 .env 是在本模块导入之后才载入的, 因此需在配置就绪后调用一次,
    否则会读到空的旧值。GitHub Actions 下调用也无副作用。
    """
    global ACCOUNT_ID, ACCESS_KEY, SECRET_KEY, BUCKET, PUBLIC_URL, PREFIX
    ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
    ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    BUCKET = os.environ.get("R2_BUCKET", "").strip()
    PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")
    PREFIX = os.environ.get("R2_PREFIX", "douyin").strip().strip("/")


def enabled() -> bool:
    """R2 是否完整配置。"""
    return bool(ACCOUNT_ID and ACCESS_KEY and SECRET_KEY and BUCKET and PUBLIC_URL)


def client():
    """懒加载 S3 客户端(R2 私有端点)。"""
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _client


def public_url(key: str) -> str:
    return f"{PUBLIC_URL}/{key}"


def content_type(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


def upload(local_path: Path, key: str) -> str:
    """上传单个文件, 成功返回公开 URL, 失败返回 ''。"""
    try:
        extra = {"ContentType": content_type(local_path)}
        client().upload_file(str(local_path), BUCKET, key, ExtraArgs=extra)
        return public_url(key)
    except Exception as e:
        print(f"    !! R2 上传失败 {key}: {type(e).__name__}: {e}")
        return ""
