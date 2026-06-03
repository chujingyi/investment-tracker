import argparse
import json
import re
from datetime import datetime
from pathlib import Path


DELTA_RE = re.compile(r"Δ\s*[：:]?\s*([+-]?\d+)")
TIME_RE = re.compile(r"^\[(.*?)\]")


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        config = json.load(f)
    required = ["campaign_id", "events"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"config missing keys: {', '.join(missing)}")
    return config


def resolve_campaign_dir(config_path: Path) -> Path:
    return config_path.resolve().parent


def parse_logs(config: dict, logs_dir: Path) -> dict[str, list[dict]]:
    records = {event: [] for event in config["events"]}
    sites = ["cn", "kr", "jp", "gl"]

    for event in config["events"]:
        for site in sites:
            path = logs_dir / f"{site}_{event}.log"
            if not path.exists():
                continue

            with path.open(encoding="utf-8") as f:
                for line in f:
                    delta_match = DELTA_RE.search(line)
                    if not delta_match:
                        continue

                    time_match = TIME_RE.search(line)
                    if not time_match:
                        continue

                    records[event].append(
                        {
                            "time": time_match.group(1),
                            "source": site.upper(),
                            "delta": int(delta_match.group(1)),
                        }
                    )
    return records


def sort_campaign(config_path: Path) -> str:
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    campaign_dir = resolve_campaign_dir(config_path)
    logs_dir = campaign_dir / config.get("logs_dir", "logs")
    exports_dir = campaign_dir / config.get("exports_dir", "exports")
    exports_dir.mkdir(parents=True, exist_ok=True)

    records = parse_logs(config, logs_dir)
    export_names = config.get("exports", {})
    export_date = datetime.now().strftime("%Y%m%d")
    summaries = []

    for event, items in records.items():
        if not items:
            summaries.append(f"{event}: 暂无变化记录")
            continue

        items.sort(key=lambda item: item["delta"], reverse=True)
        base_output_name = export_names.get(event, f"{event}_rank.log")
        stem = Path(base_output_name).stem
        suffix = Path(base_output_name).suffix or ".log"
        output_name = f"{export_date}_{stem}{suffix}"

        with (exports_dir / output_name).open("w", encoding="utf-8") as f:
            for item in items:
                f.write(
                    f"{item['time']} - 应募【{event}】，来源【{item['source']}】，变化差值: {item['delta']}\n"
                )

        lines = [f"{event}: {len(items)} 条变化记录，已生成 {output_name}"]
        for item in items[:20]:
            lines.append(f"  {item['time']} [{item['source']}] {item['delta']:+d}")
        if len(items) > 20:
            lines.append("  ...")
        summaries.append("\n".join(lines))

    title = config.get("name") or config.get("campaign_id") or campaign_dir.name
    return f"K4 排序报告：{title}\n\n" + "\n\n".join(summaries)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to campaign JSON config")
    args = parser.parse_args()

    print(sort_campaign(Path(args.config)))


if __name__ == "__main__":
    main()
