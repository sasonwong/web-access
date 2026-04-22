#!/usr/bin/env python3
"""根据用户输入匹配站点经验文件（跨平台，纯 Python，无 Node 依赖）
用法：python match-site.py "用户输入文本"
输出：匹配到的站点经验内容，无匹配则静默
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_DIR = ROOT / "references" / "site-patterns"


def main():
    query = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not query or not PATTERNS_DIR.exists():
        sys.exit(0)

    for path in sorted(PATTERNS_DIR.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue

        domain = path.stem
        raw = path.read_text(encoding="utf-8")

        # 提取 aliases
        aliases: list[str] = []
        for line in raw.splitlines():
            if line.startswith("aliases:"):
                value = line[len("aliases:"):].strip()
                value = value.strip("[]")
                aliases = [v.strip() for v in value.split(",") if v.strip()]
                break

        # 构建匹配模式
        candidates = [domain] + aliases
        escaped = [re.escape(t) for t in candidates]
        pattern = "|".join(escaped)
        if not re.search(pattern, query, re.IGNORECASE):
            continue

        # 跳过 frontmatter，输出正文
        fences = [m.start() for m in re.finditer(r"^---\s*$", raw, re.MULTILINE)]
        if len(fences) >= 2:
            body = raw[fences[1] + 3:].lstrip("\r\n")
        else:
            body = raw

        sys.stdout.write(f"--- 站点经验: {domain} ---\n")
        sys.stdout.write(body.rstrip() + "\n\n")


if __name__ == "__main__":
    main()
