#!/usr/bin/env python3
"""
对指定活动的监控日志按库存变化差值排序
用法:
  python3 imini_sort.py --folder <路径>   # 单个活动
  python3 imini_sort.py --all             # 所有活动
"""

import argparse
import json
import re
from pathlib import Path

from config import MONITORS_DIR

# 新格式：日志行含来源标签 [xxx]
PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - \[([^\]]+)\] 库存变化: \d+ -> \d+ \(差值: (-?\d+)\)"
)
# 旧格式（兼容）：日志行不含来源标签
PATTERN_LEGACY = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - 库存变化: \d+ -> \d+ \(差值: (-?\d+)\)"
)


def _read_info(folder: Path) -> dict:
    info_file = folder / "info.json"
    if not info_file.exists():
        return {}
    try:
        return json.loads(info_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _source_label(name: str) -> str:
    if "非大陆地址" in name:
        return "非大陆地址"
    if "大陆地址" in name:
        return "大陆地址"
    return "未知来源"


def _group_title(name: str) -> str:
    title = re.sub(r"【\s*(?:非)?大陆地址\s*】", "", name)
    return re.sub(r"\s+", " ", title).strip()


def _group_key(info: dict) -> tuple:
    return (
        (info.get("end_time") or "")[:10],
        _group_title(info.get("name", "")),
    )


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w一-鿿]", "", text)[:80] or "未命名活动"


def _group_folders(folder: Path) -> list:
    info = _read_info(folder)
    if not info:
        return [folder]

    key = _group_key(info)
    folders = []
    for candidate in sorted(Path(MONITORS_DIR).iterdir()):
        if not candidate.is_dir():
            continue
        if _group_key(_read_info(candidate)) == key:
            folders.append(candidate)
    return folders or [folder]


def _read_changes(folder: Path) -> list:
    """读取日志，返回 [(ts, source, diff), ...]，兼容新旧格式"""
    info = _read_info(folder)
    fallback_source = _source_label(info.get("name", folder.name))
    log_file = folder / f"{folder.name}.log"
    rows = []

    if not log_file.exists():
        return rows

    for enc in ("utf-8", "gbk", "latin1"):
        try:
            with open(log_file, encoding=enc) as f:
                for line in f:
                    m = PATTERN.search(line)
                    if m:
                        rows.append((m.group(1), m.group(2), int(m.group(3))))
                        continue
                    m = PATTERN_LEGACY.search(line)
                    if m:
                        rows.append((m.group(1), fallback_source, int(m.group(2))))
            break
        except UnicodeDecodeError:
            continue

    return rows


def sort_folder(folder: Path) -> str:
    """单文件排序（用于 --folder 模式）"""
    rows = _read_changes(folder)

    if not rows:
        log_file = folder / f"{folder.name}.log"
        if not log_file.exists():
            return f"❌ 找不到日志文件: {log_file.name}"
        return f"📭 {folder.name}：暂无库存变化记录"

    rows.sort(key=lambda x: x[2])  # sort by diff

    out_file = folder / f"{folder.name}_sorted.log"
    with open(out_file, "w", encoding="utf-8") as f:
        for ts, source, diff in rows:
            f.write(f"{ts} 【{source}】 库存变化：{diff}\n")

    lines = [f"📊 {folder.name}\n共 {len(rows)} 条变化记录："]
    for ts, source, diff in rows[:20]:
        lines.append(f"  {ts} 【{source}】  {diff:+d}")
    if len(rows) > 20:
        lines.append(f"  ...（已截断，完整结果见服务器 {out_file.name}）")

    return "\n".join(lines)


def sort_group_for_folder(folder: Path) -> str:
    """合并同一活动组的所有日志并排序"""
    folders = _group_folders(folder)
    base_info = _read_info(folder)
    title = _group_title(base_info.get("name", folder.name))
    date_part = (base_info.get("end_time") or "")[:10] or "未知截止"

    rows = []       # (diff, ts, source)
    missing_logs = []
    for item in folders:
        changes = _read_changes(item)
        if not changes:
            log_file = item / f"{item.name}.log"
            if not log_file.exists():
                missing_logs.append(item.name)
            continue
        for ts, source, diff in changes:
            rows.append((diff, ts, source))

    if not rows:
        if missing_logs:
            return f"❌ 找不到日志文件: {', '.join(missing_logs)}"
        return f"📭 {date_part}_{title}：暂无库存变化记录"

    rows.sort(key=lambda x: x[0])

    out_file = Path(MONITORS_DIR) / f"{date_part}_{_safe_name(title)}_合并排序.log"
    with open(out_file, "w", encoding="utf-8") as f:
        for diff, ts, source in rows:
            f.write(f"{ts} 【{source}】 库存变化：{diff}\n")

    lines = [f"📊 {date_part}_{title}\n共 {len(rows)} 条变化记录："]
    for diff, ts, source in rows[:20]:
        lines.append(f"  {ts} 【{source}】 库存变化：{diff}")
    if len(rows) > 20:
        lines.append(f"  ...（已截断，完整结果见服务器 {out_file.name}）")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--folder", help="指定活动文件夹路径")
    group.add_argument("--all",    action="store_true", help="处理所有活动")
    args = parser.parse_args()

    if args.all:
        results = []
        seen = set()
        for f in sorted(Path(MONITORS_DIR).iterdir()):
            if not f.is_dir():
                continue
            key = _group_key(_read_info(f))
            if key in seen:
                continue
            seen.add(key)
            results.append(sort_group_for_folder(f))
        print("\n\n".join(results) if results else "暂无监控文件夹")
    else:
        print(sort_group_for_folder(Path(args.folder)))


if __name__ == "__main__":
    main()
