#!/usr/bin/env python3
"""find-url - 从本地 Chrome 书签/历史中检索 URL。

用于定位公网搜索覆盖不到的目标（组织内部系统、SSO 后台、内网域名等）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


WEBKIT_EPOCH_DIFF_US = 11644473600000000


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    if "WSL_INTEROP" in os.environ:
        return True
    try:
        release = os.uname().release.lower()
        if "microsoft" in release or "wsl" in release:
            return True
    except Exception:
        pass
    return False


def die(message: str) -> "NoReturn":
    print(message, file=sys.stderr)
    raise SystemExit(1)


def parse_since(value: str) -> datetime:
    if not value:
        die("--since 需要值")
    if len(value) >= 2 and value[:-1].isdigit() and value[-1] in {"d", "h", "m"}:
        amount = int(value[:-1])
        unit = value[-1]
        delta = {
            "d": timedelta(days=amount),
            "h": timedelta(hours=amount),
            "m": timedelta(minutes=amount),
        }[unit]
        return datetime.now() - delta

    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    die(f"无效 --since 值: {value}（用 1d / 7h / 30m / YYYY-MM-DD）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find-url.py",
        description="从本地 Chrome 书签/历史中检索 URL",
        add_help=True,
    )
    parser.add_argument("keywords", nargs="*", help="空格分词、多词 AND，匹配 title + url")
    parser.add_argument("--only", choices=["bookmarks", "history"], default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--since", type=parse_since, default=None)
    parser.add_argument("--sort", choices=["recent", "visits"], default="recent")
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit 需为非负整数")
    return args


def get_chrome_data_dir() -> Path | None:
    env_dir = os.environ.get("CHROME_USER_DATA_DIR")
    if env_dir:
        path = Path(env_dir)
        if path.exists():
            return path

    home = Path.home()
    if sys.platform == "darwin":
        candidates = [
            home / "Library/Application Support/Google/Chrome",
            home / "Library/Application Support/Chromium",
            home / "Library/Application Support/Microsoft Edge",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]
    if sys.platform.startswith("linux"):
        candidates = [
            home / ".config/google-chrome",
            home / ".config/chromium",
            home / ".config/microsoft-edge",
        ]
        wsl = is_wsl()
        if wsl:
            local_app_data = get_windows_local_app_data()
            if local_app_data is not None:
                candidates.extend(
                    [
                        local_app_data / "Google/Chrome/User Data",
                        local_app_data / "Chromium/User Data",
                        local_app_data / "Microsoft/Edge/User Data",
                    ]
                )
            try:
                users_dir = Path("/mnt/c/Users")
                if users_dir.exists():
                    for user_d in users_dir.iterdir():
                        if user_d.is_dir():
                            candidates.extend(
                                [
                                    user_d / "AppData/Local/Google/Chrome/User Data",
                                    user_d / "AppData/Local/Microsoft/Edge/User Data",
                                ]
                            )
            except PermissionError:
                pass
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates = [
                Path(local_app_data) / "Google/Chrome/User Data",
                Path(local_app_data) / "Chromium/User Data",
                Path(local_app_data) / "Microsoft/Edge/User Data",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return candidate
            return candidates[0]
    return None


def get_windows_local_app_data() -> Path | None:
    commands = [
        ["cmd.exe", "/C", "echo", "%LOCALAPPDATA%"],
        ["powershell.exe", "-NoProfile", "-Command", "[Environment]::GetFolderPath('LocalApplicationData')"],
    ]
    for command in commands:
        try:
            output = subprocess.check_output(command, stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            continue
        path = windows_path_to_unix(output)
        if path is not None:
            return path
    return None


def windows_path_to_unix(value: str) -> Path | None:
    normalized = value.strip().strip('"')
    if len(normalized) < 3 or normalized[1:3] != ':\\':
        return None
    drive = normalized[0].lower()
    suffix = normalized[3:].replace('\\', '/')
    return Path(f"/mnt/{drive}/{suffix}")


def list_profiles(data_dir: Path) -> list[SimpleNamespace]:
    local_state = data_dir / "Local State"
    try:
        state = json.loads(local_state.read_text(encoding="utf-8"))
        info_cache = state.get("profile", {}).get("info_cache", {})
        profiles = [
            SimpleNamespace(dir_name=dir_name, profile_name=(info.get("name") or dir_name))
            for dir_name, info in info_cache.items()
        ]
        if profiles:
            return profiles
    except Exception:
        pass
    return [SimpleNamespace(dir_name="Default", profile_name="Default")]


def search_bookmarks(profile_dir: Path, profile_name: str, keywords: list[str]) -> list[dict[str, str]]:
    bookmark_file = profile_dir / "Bookmarks"
    if not bookmark_file.exists() or not keywords:
        return []

    try:
        data = json.loads(bookmark_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    needles = [keyword.lower() for keyword in keywords]
    results: list[dict[str, str]] = []

    def walk(node: dict, trail: list[str]) -> None:
        if not node:
            return
        if node.get("type") == "url":
            haystack = f"{node.get('name', '')} {node.get('url', '')}".lower()
            if all(needle in haystack for needle in needles):
                results.append(
                    {
                        "profile": profile_name,
                        "name": node.get("name", ""),
                        "url": node.get("url", ""),
                        "folder": " / ".join(trail),
                    }
                )
        children = node.get("children")
        if isinstance(children, list):
            next_trail = trail + ([node["name"]] if node.get("name") else [])
            for child in children:
                walk(child, next_trail)

    for root in (data.get("roots") or {}).values():
        if isinstance(root, dict):
            walk(root, [])
    return results


def webkit_us_from_datetime(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000) + WEBKIT_EPOCH_DIFF_US


def format_visit_time(webkit_us: int) -> str:
    unix_us = (webkit_us - WEBKIT_EPOCH_DIFF_US) / 1_000_000
    return datetime.fromtimestamp(unix_us).strftime("%Y-%m-%d %H:%M:%S")


def search_history(
    profile_dir: Path,
    profile_name: str,
    keywords: list[str],
    since: datetime | None,
    limit: int,
    sort: str,
) -> list[dict[str, object]]:
    source_db = profile_dir / "History"
    if not source_db.exists():
        return []

    temp_dir = Path(tempfile.gettempdir())
    temp_db = temp_dir / f"chrome-history-{os.getpid()}-{int(datetime.now().timestamp() * 1000)}.sqlite"
    try:
        shutil.copy2(source_db, temp_db)
        connection = sqlite3.connect(temp_db)
        try:
            conditions = ["last_visit_time > 0"]
            params: list[object] = []
            for keyword in keywords:
                conditions.append("LOWER(COALESCE(title, '') || ' ' || COALESCE(url, '')) LIKE ?")
                params.append(f"%{keyword.lower()}%")
            if since is not None:
                conditions.append("last_visit_time >= ?")
                params.append(webkit_us_from_datetime(since))

            order_by = "visit_count DESC, last_visit_time DESC" if sort == "visits" else "last_visit_time DESC"
            sql = (
                "SELECT title, url, last_visit_time, visit_count "
                "FROM urls WHERE " + " AND ".join(conditions) + f" ORDER BY {order_by}"
            )
            if limit != 0:
                sql += " LIMIT ?"
                params.append(limit)

            rows = connection.execute(sql, params).fetchall()
            return [
                {
                    "profile": profile_name,
                    "title": row[0] or "",
                    "url": row[1] or "",
                    "visit": format_visit_time(int(row[2])),
                    "visit_count": int(row[3] or 0),
                }
                for row in rows
            ]
        finally:
            connection.close()
    except sqlite3.Error as e:
        print(f"历史数据库查询失败: {e}", file=sys.stderr)
        return []
    finally:
        try:
            temp_db.unlink()
        except FileNotFoundError:
            pass


def clean(text: object) -> str:
    return str(text or "").replace("|", "│").strip()


def print_bookmarks(items: list[dict[str, str]], multi_profile: bool) -> None:
    print(f"[书签] {len(items)} 条")
    for item in items:
        segments = [clean(item.get("name")) or "(无标题)", clean(item.get("url"))]
        if item.get("folder"):
            segments.append(clean(item.get("folder")))
        if multi_profile:
            segments.append("@" + clean(item.get("profile")))
        print("  " + " | ".join(segments))


def print_history(items: list[dict[str, object]], multi_profile: bool, sort_label: str) -> None:
    print(f"[历史] {len(items)} 条（{sort_label}）")
    for item in items:
        segments = [
            clean(item.get("title")) or "(无标题)",
            clean(item.get("url")),
            clean(item.get("visit")),
        ]
        if int(item.get("visit_count") or 0) > 1:
            segments.append(f"visits={int(item['visit_count'])}")
        if multi_profile:
            segments.append("@" + clean(item.get("profile")))
        print("  " + " | ".join(segments))


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    data_dir = get_chrome_data_dir()
    if data_dir is None or not data_dir.exists():
        die("未找到可用浏览器用户数据目录（已尝试 Chrome/Edge）。可通过 CHROME_USER_DATA_DIR 指定目录，例如 /mnt/c/Users/<用户名>/AppData/Local/Microsoft/Edge/User Data。")

    profiles = list_profiles(data_dir)
    do_bookmarks = args.only != "history"
    do_history = args.only != "bookmarks"

    bookmarks: list[dict[str, str]] = []
    history: list[dict[str, object]] = []
    per_profile_limit = 0 if args.limit == 0 else args.limit * 2

    for profile in profiles:
        profile_dir = data_dir / profile.dir_name
        if not profile_dir.exists():
            continue
        if do_bookmarks:
            bookmarks.extend(search_bookmarks(profile_dir, profile.profile_name, args.keywords))
        if do_history:
            history.extend(
                search_history(
                    profile_dir,
                    profile.profile_name,
                    args.keywords,
                    args.since,
                    per_profile_limit,
                    args.sort,
                )
            )

    if args.sort == "visits":
        history.sort(key=lambda item: (-(int(item.get("visit_count") or 0)), str(item.get("visit") or "")), reverse=False)
        history.sort(key=lambda item: (int(item.get("visit_count") or 0), str(item.get("visit") or "")), reverse=True)
    else:
        history.sort(key=lambda item: str(item.get("visit") or ""), reverse=True)

    bookmarks_out = bookmarks if args.limit == 0 else bookmarks[: args.limit]
    history_out = history if args.limit == 0 else history[: args.limit]

    seen_profiles = {
        item.get("profile") for item in [*bookmarks_out, *history_out] if item.get("profile")
    }
    show_profile = len(seen_profiles) > 1

    if do_bookmarks:
        print_bookmarks(bookmarks_out, show_profile)
    if do_bookmarks and do_history:
        print()
    if do_history:
        sort_label = "按访问次数" if args.sort == "visits" else "按最近访问"
        print_history(history_out, show_profile, sort_label)

    if not args.keywords and do_bookmarks and not do_history:
        print("\n提示：书签无时间维度，无关键词查询无意义。加关键词或切换 --only history。", file=sys.stderr)

    return 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())