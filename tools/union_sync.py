#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
综合仓服务器侧合并（在 GitHub Actions 内运行，不占本地带宽）

用法：
  python3 tools/union_sync.py --union <综合仓工作副本> --src <源仓根目录> \
      [--config tools/sources.json] [--reason 触发原因]

合并规则（与容器侧旧实现一致）：
  ① 文件只增不删：综合仓内已入库的路径永不删除
  ② 同路径内容不同 → 覆盖；被覆盖的旧字节转存 rescue/<YYYYMMDD>/<线名>/<原路径>
  ③ 综合仓自家元数据（README.md / icons/market.png / check_market.py）
     固定取第一条线，后线同名文件转存（不覆盖招牌）
  ④ market.json 按 id 合并：同 id 取新 / 新 id 追加 / 线里删掉的 id 保留 / updatedAt 取最新
"""
import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time

PROTECT = ("README.md", "icons/market.png", "check_market.py")


def log(msg):
    print(msg, flush=True)


def git(args, cwd=None):
    p = subprocess.run(["git"] + args, cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError("git %s 失败: %s" % (" ".join(args), (p.stdout or "").strip()[:600]))
    return p.stdout or ""


def same(a, b):
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def archive(arch_path, src_file):
    """转存字节；已存在且一致则不重复写。返回 1=写了 0=跳过"""
    if os.path.exists(arch_path) and same(arch_path, src_file):
        return 0
    os.makedirs(os.path.dirname(arch_path), exist_ok=True)
    shutil.copy2(src_file, arch_path)
    return 1


# Directories never mirrored from a source repo:
#   .git    - VCS noise.
#   .github - would drag a source repo's own CI workflows into this repo. Adding
#             or editing a workflow file requires the `workflows` permission, which
#             the Actions token does not have, so one upstream .github/workflows/*.yml
#             made EVERY union push fail with
#             "refusing to allow a GitHub App to create or update workflow ... without
#             workflows permission" (field report: 10 consecutive failures from
#             2026-11-06 to 2026-11-09, market.json frozen the whole time).
SKIP_DIRS = (".git", ".github")


def walk_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            yield os.path.relpath(full, root).replace(os.sep, "/"), full


def load_doc(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--union", required=True, help="综合仓工作副本目录")
    ap.add_argument("--src", required=True, help="各线源仓 clone 的根目录")
    ap.add_argument("--config", default="tools/sources.json", help="监控清单配置")
    ap.add_argument("--reason", default="同步后", help="触发原因（只进日志/提交信息）")
    a = ap.parse_args()

    union = os.path.abspath(a.union)
    src_root = os.path.abspath(a.src)
    with open(a.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    lines = cfg.get("lines") or []
    if not lines:
        log("❌ 配置里没有 lines，退出")
        return 1
    name = cfg.get("name") or "DsPlayer 综合插件市场"
    desc = cfg.get("desc") or "dsplayer 插件市场（上游两条线合并镜像 · 只增不删 · 上游删包也能装）"
    today = time.strftime("%Y%m%d")

    base_doc = load_doc(os.path.join(union, "market.json")) or {}
    base = base_doc.get("plugins") or []
    stamps = [base_doc.get("updatedAt") or ""]
    logo = base_doc.get("logo") or None

    added = over = kept = archn = 0
    report = []

    for idx, line in enumerate(lines):
        repo = line["repo"]
        branch = line.get("branch", "main")
        label = line.get("label", repo)
        src = os.path.join(src_root, "line%d" % idx)
        shutil.rmtree(src, ignore_errors=True)
        url = "https://github.com/%s.git" % repo
        log("  ↳ 拉取 %s (%s) …" % (repo, branch))
        try:
            git(["clone", "--depth", "1", "--single-branch", "--branch", branch, "-q", url, src])
        except RuntimeError as e:
            log("  ⚠️ %s 拉取失败，本轮跳过该线（已在库内容保持不动）: %s" % (label, e))
            continue

        nfile = 0
        safe = label.replace("/", "_")
        for rel, full in walk_files(src):
            if rel == "market.json":
                continue
            dest = os.path.join(union, rel)
            if not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest) or union, exist_ok=True)
                shutil.copy2(full, dest)
                added += 1
                nfile += 1
            elif not same(full, dest):
                arch = os.path.join(union, "rescue", today, safe, rel)
                if idx != 0 and rel in PROTECT:
                    archn += archive(arch, full)
                else:
                    archn += archive(arch, dest)
                    shutil.copy2(full, dest)
                    over += 1
                    nfile += 1
            else:
                kept += 1

        doc = load_doc(os.path.join(src, "market.json"))
        if doc:
            inc = doc.get("plugins") or []
            index = {}
            for i, e in enumerate(base):
                if isinstance(e, dict) and e.get("id"):
                    index[e["id"]] = i
            nnew = nupd = 0
            for e in inc:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                pid = e["id"]
                if pid in index:
                    base[index[pid]] = e
                    nupd += 1
                else:
                    base.append(e)
                    index[pid] = len(base) - 1
                    nnew += 1
            stamps.append(doc.get("updatedAt") or "")
            if idx == 0 and doc.get("logo"):
                logo = doc["logo"]
            report.append("  · %s：新增文件 %d，索引 +%d / 覆盖 %d（现 %d 条）" % (label, nfile, nnew, nupd, len(base)))
        else:
            report.append("  · %s：未读到 market.json（跳过索引合并，文件 %d 个）" % (label, nfile))

        shutil.rmtree(src, ignore_errors=True)

    stamps = sorted([s for s in stamps if s])
    updated = stamps[-1] if stamps else time.strftime("%Y-%m-%d")

    out = {
        "name": name,
        "desc": desc,
        "logo": logo or "icons/market.png",
        "updatedAt": updated,
        "plugins": base,
    }
    tmp = os.path.join(union, ".market.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, os.path.join(union, "market.json"))

    log("📦 合并完成（%s）：文件 新增 %d · 覆盖 %d · 原样 %d · 归档 %d ｜ 索引 %d 条 ｜ updatedAt %s"
        % (a.reason, added, over, kept, archn, len(base), updated))
    for r in report:
        log(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
