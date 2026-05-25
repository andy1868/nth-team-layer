"""
Blackboard CLI — 命令行工具实现

子命令：
    list      列出条目（可过滤 scope/status/author/topic）
    view      Kanban 视图
    post      新建条目
    update    更新条目状态/内容
    history   查看版本历史
    get       获取最新版本
"""

import argparse
import json
import sys
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from .blackboard import Blackboard
from .scope import Scope
from .views import render_kanban, render_table


def cmd_list(args):
    bb = Blackboard(args.root)
    scope = Scope.parse(args.scope) if args.scope else None
    entries = bb.list(
        scope=scope,
        status=args.status,
        author=args.author,
        topic_contains=args.topic,
    )
    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False))
    else:
        print(render_table(entries))
        print(f"\n({len(entries)} entries)")


def cmd_view(args):
    bb = Blackboard(args.root)
    scope = Scope.parse(args.scope) if args.scope else None
    entries = bb.list(scope=scope, author=args.author)
    print(f"\nBlackboard — scope: {args.scope or 'ALL'}\n")
    print(render_kanban(entries, width=args.width))


def cmd_post(args):
    bb = Blackboard(args.root)
    entry = bb.post(
        topic=args.topic,
        author=args.author,
        scope=args.scope,
        status=args.status,
        content=args.content or "",
        metadata=json.loads(args.metadata) if args.metadata else None,
    )
    print(f"Posted entry {entry.id}")
    if args.json:
        print(json.dumps(entry.to_dict(), indent=2, ensure_ascii=False))


def cmd_update(args):
    bb = Blackboard(args.root)
    try:
        new_entry = bb.update(
            entry_id=args.entry_id,
            author=args.author,
            status=args.status,
            content=args.content,
            topic=args.topic,
            metadata_patch=json.loads(args.metadata) if args.metadata else None,
            scope=args.scope,
        )
        print(f"Updated {new_entry.id} → version {new_entry.version}")
        if args.json:
            print(json.dumps(new_entry.to_dict(), indent=2, ensure_ascii=False))
    except (ValueError, PermissionError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_get(args):
    bb = Blackboard(args.root)
    scope = Scope.parse(args.scope) if args.scope else None
    entry = bb.get(args.entry_id, scope)
    if entry is None:
        print(f"Entry {args.entry_id} not found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(entry.to_dict(), indent=2, ensure_ascii=False))


def cmd_history(args):
    bb = Blackboard(args.root)
    versions = bb.history(args.entry_id)
    if not versions:
        print(f"No history for {args.entry_id}", file=sys.stderr)
        sys.exit(1)
    print(f"History for {args.entry_id} ({len(versions)} versions):\n")
    for v in versions:
        print(f"  v{v.version:2d} @ {v.updated_at[:19]}  [{v.status:7s}] "
              f"{v.topic}  (by {v.author})")


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m team_layer.blackboard",
        description="Blackboard CLI — multi-agent shared workspace",
    )
    p.add_argument("--root", default="blackboard", help="blackboard root dir (default: ./blackboard)")
    sub = p.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="list entries")
    p_list.add_argument("--scope", help="filter by scope (shared / group:X / private:X)")
    p_list.add_argument("--status", help="filter by status")
    p_list.add_argument("--author", help="filter by author")
    p_list.add_argument("--topic", help="filter by topic substring")
    p_list.add_argument("--json", action="store_true", help="output JSON")
    p_list.set_defaults(func=cmd_list)

    # view (kanban)
    p_view = sub.add_parser("view", help="kanban view")
    p_view.add_argument("--scope", help="scope to render")
    p_view.add_argument("--author", help="filter by author")
    p_view.add_argument("--width", type=int, default=28, help="column width")
    p_view.set_defaults(func=cmd_view)

    # post
    p_post = sub.add_parser("post", help="create new entry")
    p_post.add_argument("topic", help="entry topic")
    p_post.add_argument("--author", required=True, help="author (agent_id or username)")
    p_post.add_argument("--scope", default="shared", help="scope (default: shared)")
    p_post.add_argument("--status", default="todo", help="initial status")
    p_post.add_argument("--content", help="detailed content")
    p_post.add_argument("--metadata", help="JSON dict of metadata")
    p_post.add_argument("--json", action="store_true", help="output JSON")
    p_post.set_defaults(func=cmd_post)

    # update
    p_upd = sub.add_parser("update", help="update entry (appends new version)")
    p_upd.add_argument("entry_id", help="entry id (or prefix)")
    p_upd.add_argument("--author", required=True, help="updater identity")
    p_upd.add_argument("--status", help="new status")
    p_upd.add_argument("--content", help="new content")
    p_upd.add_argument("--topic", help="new topic")
    p_upd.add_argument("--metadata", help="JSON dict to merge into metadata")
    p_upd.add_argument("--scope", help="scope (auto-detected if omitted)")
    p_upd.add_argument("--json", action="store_true", help="output JSON")
    p_upd.set_defaults(func=cmd_update)

    # get
    p_get = sub.add_parser("get", help="get latest version of an entry")
    p_get.add_argument("entry_id")
    p_get.add_argument("--scope")
    p_get.set_defaults(func=cmd_get)

    # history
    p_hist = sub.add_parser("history", help="show version history")
    p_hist.add_argument("entry_id")
    p_hist.set_defaults(func=cmd_history)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
