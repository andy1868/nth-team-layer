"""
Views — 黑板视图渲染

提供两种纯文本视图（无第三方依赖）：
- render_kanban: 三栏 Kanban (TODO / DOING / DONE)
- render_table:  通用表格列表
"""

from typing import Iterable, List, Optional

from .blackboard import BlackboardEntry


KANBAN_COLUMNS = ("todo", "doing", "done")
KANBAN_TITLES = {
    "todo": "📋 TODO",
    "doing": "🔨 DOING",
    "done": "✅ DONE",
    "blocked": "🚧 BLOCKED",
}


def render_kanban(
    entries: Iterable[BlackboardEntry],
    width: int = 28,
    show_blocked: bool = True,
) -> str:
    """
    渲染三栏（或四栏）Kanban。

    Args:
        entries: 要展示的条目（通常已按 scope 过滤）
        width: 每栏宽度（字符）
        show_blocked: 是否显示 BLOCKED 栏
    """
    columns = list(KANBAN_COLUMNS)
    if show_blocked and any(e.status == "blocked" for e in entries):
        columns.append("blocked")

    # 按 status 分组
    buckets = {col: [] for col in columns}
    other_count = 0
    for entry in entries:
        if entry.status in buckets:
            buckets[entry.status].append(entry)
        else:
            other_count += 1

    # 排序：updated_at 倒序
    for col in columns:
        buckets[col].sort(key=lambda e: e.updated_at, reverse=True)

    # 渲染
    sep = "+" + ("-" * width + "+") * len(columns)
    lines = [sep]

    # 标题行
    headers = []
    for col in columns:
        title = KANBAN_TITLES.get(col, col.upper())
        count = len(buckets[col])
        label = f" {title} ({count})"
        headers.append(label[:width].ljust(width))
    lines.append("|" + "|".join(headers) + "|")
    lines.append(sep)

    # 内容行：找到最长的列
    max_rows = max(len(buckets[col]) for col in columns) if buckets else 0
    if max_rows == 0:
        empty_row = "|" + "|".join(" (empty)".ljust(width) for _ in columns) + "|"
        lines.append(empty_row)
    else:
        for row_idx in range(max_rows):
            cells = []
            for col in columns:
                if row_idx < len(buckets[col]):
                    cell = _format_cell(buckets[col][row_idx], width)
                else:
                    cell = " " * width
                cells.append(cell)
            lines.append("|" + "|".join(cells) + "|")

    lines.append(sep)

    if other_count:
        lines.append(f"  ({other_count} entries with non-standard status hidden)")

    return "\n".join(lines)


def _format_cell(entry: BlackboardEntry, width: int) -> str:
    """单元格格式：' topic (author)' 截断到 width"""
    label = f" {entry.topic} ({entry.author})"
    if len(label) > width:
        label = label[: width - 1] + "…"
    return label.ljust(width)


def render_table(
    entries: List[BlackboardEntry],
    columns: tuple = ("id", "scope", "status", "topic", "author", "updated_at"),
    max_topic_len: int = 40,
) -> str:
    """简单的 ASCII 表格列表"""
    if not entries:
        return "(no entries)"

    # 计算每列宽度
    widths = {col: len(col) for col in columns}
    for e in entries:
        for col in columns:
            value = _value(e, col, max_topic_len)
            widths[col] = max(widths[col], len(value))

    # 渲染
    def row(values):
        return " | ".join(v.ljust(widths[col]) for col, v in zip(columns, values))

    sep_line = "-+-".join("-" * widths[col] for col in columns)
    lines = [row(columns), sep_line]
    for e in entries:
        lines.append(row([_value(e, col, max_topic_len) for col in columns]))
    return "\n".join(lines)


def _value(entry: BlackboardEntry, col: str, max_topic_len: int) -> str:
    """字段提取 + 字符串化"""
    if col == "topic":
        val = entry.topic
        if len(val) > max_topic_len:
            val = val[: max_topic_len - 1] + "…"
        return val
    if col == "updated_at":
        # 只展示日期 + 时分
        return entry.updated_at[:16].replace("T", " ")
    if col == "id":
        return entry.id[:8]  # 缩短
    return str(getattr(entry, col, ""))
