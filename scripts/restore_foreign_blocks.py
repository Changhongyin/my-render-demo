# -*- coding: utf-8 -*-
"""防御网：把被行情脚本吞掉的「别人写的」顶层块（weather / nbs）从备份里补回来。

背景：`local_spider_v2.py` 只负责 `meta` / `crops`，它重建 data.json 时如果没保留其它顶层键，
就会把 `weather`（气象预警，fetch_weather_alerts.py）和 `nbs`（国家统计局，fetch_nbs_prices.py）
整块吞掉 —— 前端橙色预警卡、蓝色官方参考卡第二天就消失了。

本脚本做的事（**只增不改**，幂等，可反复跑）：
  1. 读当前 data.json 与「行情脚本写入前的自动备份」backups/data_YYYY_MM_DD.json
  2. 备份里有、当前没有的顶层键 → 原样补回来（weather / nbs）
  3. 行情脚本重建 crops 时丢掉的预警字段（crops[].alerts / alerts_source / alerts_updated_at）
     在"当前为空或缺失"时补回来 —— 有值的一律不动，绝不用旧数据覆盖新数据
  4. 同步写回 data.json 与 public/data.json（原子写入 + 权限 644）

用法：
    python3 scripts/restore_foreign_blocks.py                 # 用最新的行情备份
    python3 scripts/restore_foreign_blocks.py backups/xxx.json # 指定备份
"""

import json
import os
import re
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILES = [os.path.join(BASE_DIR, "data.json"),
                os.path.join(BASE_DIR, "public", "data.json")]
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP_KEYS = ("meta", "crops")                      # 这两个键由行情脚本负责，永远以"当前"为准
BACKUP_RE = re.compile(r"^data_\d{4}_\d{2}_\d{2}\.json$")


def newest_backup():
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR) if BACKUP_RE.match(n))
    except OSError:
        return None
    return os.path.join(BACKUP_DIR, names[-1]) if names else None


def save_json(payload, paths):
    for path in paths:
        folder = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".data.json.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass


def is_empty(value):
    """判断"这个字段是不是空壳"（空串 / 空数组 / 全空对象的字典 / None）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0
    if isinstance(value, dict):
        return not any(v for v in value.values() if v)
    return False


CROP_ALERT_KEYS = ("alerts", "alerts_source", "alerts_updated_at")

# 其它脚本写在 meta 上的字段前缀（与 local_spider_v2.py 的白名单保持一致）
FOREIGN_META_PREFIXES = ("weather_", "nbs_", "alert_")


def merge_meta(current, old):
    """把其它脚本写在 meta 上的字段补回来（本脚本的重建逻辑会丢掉它们）。

    只按前缀白名单补（weather_ / nbs_ / alert_），sources 按 type 去重合并，
    notes 只补以【开头的他人说明 —— 宁可少补，绝不把旧格式遗留键搬进来。
    """
    out, src = current.get("meta"), old.get("meta")
    if not isinstance(out, dict) or not isinstance(src, dict):
        return []

    restored = []
    for key, value in src.items():
        if key in ("sources", "notes"):
            continue
        if key.startswith(FOREIGN_META_PREFIXES) and key not in out:
            out[key] = value
            restored.append("meta.%s" % key)

    have_types = set(s.get("type") for s in (out.get("sources") or []) if isinstance(s, dict))
    added = [s for s in (src.get("sources") or [])
             if isinstance(s, dict) and s.get("type") and s.get("type") not in have_types]
    if added:
        out["sources"] = [s for s in (out.get("sources") or []) if isinstance(s, dict)] + added
        restored.append("meta.sources(+%d)" % len(added))

    notes = list(out.get("notes") or [])
    new_notes = [n for n in (src.get("notes") or []) if str(n).startswith("【") and n not in notes]
    if new_notes:
        out["notes"] = notes + new_notes
        restored.append("meta.notes(+%d)" % len(new_notes))
    return restored


def merge_crop_alerts(current, old):
    """行情脚本重建 crops 时丢掉的预警字段 → 在"当前为空/缺失"时从备份补回。"""
    restored = []
    old_by_name = {}
    for crop in old.get("crops") or []:
        if isinstance(crop, dict) and crop.get("name"):
            old_by_name[crop["name"]] = crop

    for crop in current.get("crops") or []:
        if not isinstance(crop, dict):
            continue
        src = old_by_name.get(crop.get("name"))
        if not src:
            continue
        for key in CROP_ALERT_KEYS:
            if key in src and is_empty(crop.get(key)):
                crop[key] = src[key]
                restored.append("%s.%s" % (crop.get("name"), key))
    return restored


def main():
    backup = sys.argv[1] if len(sys.argv) > 1 else newest_backup()
    if not backup or not os.path.exists(backup):
        print("ℹ️  找不到行情备份，跳过恢复（不用慌：说明还没发生过覆盖）")
        return 0

    with open(OUTPUT_FILES[0], encoding="utf-8") as f:
        current = json.load(f)
    with open(backup, encoding="utf-8") as f:
        old = json.load(f)

    if not isinstance(current, dict) or not isinstance(old, dict):
        print("⚠️  数据结构异常，跳过恢复")
        return 0

    restored = []
    for key, value in old.items():
        if key in KEEP_KEYS:
            continue
        if key not in current and isinstance(value, dict):   # 只补"丢了"的对象型数据块
            current[key] = value
            restored.append(key)

    meta_restored = merge_meta(current, old)
    crop_restored = merge_crop_alerts(current, old)

    if not restored and not meta_restored and not crop_restored:
        print("✅ 无需恢复（weather / nbs / meta 标注 / crops 预警字段都在）")
        return 0

    save_json(current, OUTPUT_FILES)
    if restored:
        print("🩹 已从 %s 补回被吞掉的数据块：%s"
              % (os.path.relpath(backup, BASE_DIR), "、".join(restored)))
    if meta_restored:
        print("🩹 已补回 meta 上的标注：%s" % "、".join(meta_restored))
    if crop_restored:
        print("🩹 已补回被清空的预警字段：%s" % "、".join(crop_restored))
    print("   当前顶层键：%s" % "、".join(current.keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
