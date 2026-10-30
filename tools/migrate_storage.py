#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地运行时数据统一搬到缓存根目录（默认 D:\\gitdy）。

背景：作品缓存早就在 D:\\gitdy，但另外两处仍写在 C 盘项目目录里——
  * state/        每个账号的 known/works 状态（决定哪些作品算「已抓过」）
  * run_log.txt   监控日志（持续追加，C 盘会被反复写）
  * downloads/    早期没配 DOWNLOAD_DIR 时留下的历史作品

本脚本只做「复制 + 跳过已存在」，不删除任何源文件，可反复运行（增量）。
确认无误后再自行清理旧目录。

用法:
    python tools/migrate_storage.py                 # 真实搬运
    python tools/migrate_storage.py --dry-run       # 只看会搬多少
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = Path(os.environ.get("CACHE_DIR", r"D:\gitdy"))

# (源目录, 目标目录, 说明)
TASKS = [
    (ROOT / "state", DEST / "state", "账号状态 state/"),
    (ROOT / "downloads", DEST, "历史作品 downloads/"),
]


def copy_tree(src: Path, dst: Path, dry: bool):
    copied = skipped = failed = 0
    copied_bytes = 0
    if not src.exists():
        return 0, 0, 0, 0
    for f in src.rglob("*"):
        if f.is_dir():
            continue
        rel = f.relative_to(src)
        target = dst / rel
        try:
            if target.exists() and target.stat().st_size == f.stat().st_size:
                skipped += 1
                continue
            if dry:
                copied += 1
                copied_bytes += f.stat().st_size
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".mig")
            shutil.copy2(f, tmp)
            os.replace(tmp, target)          # 原子替换, 避免半截文件
            copied += 1
            copied_bytes += f.stat().st_size
        except Exception as e:
            failed += 1
            print(f"    !! {rel}: {e}")
    return copied, skipped, failed, copied_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"目标缓存根目录: {DEST}")
    print(f"模式: {'预演(不写文件)' if args.dry_run else '真实搬运'}\n")
    tot = {"copied": 0, "skipped": 0, "failed": 0, "bytes": 0}
    for src, dst, label in TASKS:
        if not src.exists():
            print(f"- {label}: 源不存在, 跳过")
            continue
        c, s, f, b = copy_tree(src, dst, args.dry_run)
        print(f"- {label}: 复制 {c} 个 ({b/1024/1024:.1f} MB), "
              f"已存在跳过 {s} 个, 失败 {f} 个")
        tot["copied"] += c
        tot["skipped"] += s
        tot["failed"] += f
        tot["bytes"] += b
    print(f"\n合计: 复制 {tot['copied']} 个文件 "
          f"({tot['bytes']/1024/1024:.1f} MB), 跳过 {tot['skipped']}, "
          f"失败 {tot['failed']}")
    return 1 if tot["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
