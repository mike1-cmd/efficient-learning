#!/usr/bin/env python3
"""建立一次「高效学习六阶闭环」的学习会话目录。

用法:
    python new_session.py "Spring Boot 入门" --path ./learn
    python new_session.py "统计学" --path ./learn --date 2026-11-08

生成（全部来自 assets/templates/，脚本里不硬编码任何模板内容）:
    <path>/<date>-<topic>/
        00-plan.md           学习计划卡（定靶 + 六阶进度 + 复习计划）
        01-diagnose.md       诊断题 + 三栏结论
        02-core.md           20% 骨架 + 排除清单
        03-practice.md       动手清单（抄/改/造 + 费曼输出）
        04-knowledge-map.md  知识图谱模板 + 检查表 + 回测
        .stage.json          阶段状态（由 stage.py 读写）
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "assets" / "templates"

# 模板 → 生成文件。唯一事实来源是 assets/templates/，改模板即改产出。
TEMPLATES = [
    ("learning-plan.md", "00-plan.md"),
    ("diagnose.md", "01-diagnose.md"),
    ("core.md", "02-core.md"),
    ("practice.md", "03-practice.md"),
    ("knowledge-map.md", "04-knowledge-map.md"),
]


def slugify(text: str) -> str:
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    return text or "topic"


def main() -> int:
    ap = argparse.ArgumentParser(description="建立高效学习会话目录")
    ap.add_argument("topic", help="学习主题，例如 \"Spring Boot 入门\"")
    ap.add_argument("--path", "-p", default="./learn", help="输出目录（默认 ./learn）")
    ap.add_argument("--date", "-d", default=None, help="日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--force", "-f", action="store_true", help="目录已存在时覆盖")
    args = ap.parse_args()

    day = args.date or dt.date.today().isoformat()
    topic = args.topic.strip()
    root = Path(args.path).expanduser().resolve() / f"{day}-{slugify(topic)}"

    if root.exists() and any(root.iterdir()) and not args.force:
        print(f"[!] 目录已存在且非空: {root}\n    用 --force 覆盖，或换一个 --path。")
        return 1

    # 先确认模板齐全，避免建到一半才发现缺文件
    missing = [n for n, _ in TEMPLATES if not (TEMPLATE_DIR / n).is_file()]
    if missing:
        print(f"[!] 缺模板: {', '.join(missing)}\n    期望目录: {TEMPLATE_DIR}")
        return 2

    root.mkdir(parents=True, exist_ok=True)
    for name, dest in TEMPLATES:
        text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        text = (text.replace("{主题}", topic)
                    .replace("{YYYY-MM-DD}", day)
                    .replace("{第几轮}", "第 1 轮"))
        (root / dest).write_text(text, encoding="utf-8")

    (root / ".stage.json").write_text(
        json.dumps({"topic": topic, "stage": 0, "cycle": 1, "history": [], "retries": []},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[✓] 学习会话已建立: {root}")
    for f in sorted(root.iterdir()):
        print(f"    {f.name}")
    print("\n阶段状态: 第 0 阶 · 定靶（跳阶会被 stage.py 拦住）")
    print("下一步（只做这一件事）: 填 00-plan.md 的定靶四行，"
          "其中「成功标准」要写成能被别人验证的句子。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
