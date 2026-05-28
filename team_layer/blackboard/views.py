"""
Views


- render_kanban:  Kanban (TODO / DOING / DONE)
- render_table:
"""

from typing import Iterable, List, Optional

from .blackboard import BlackboardEntry


KANBAN_COLUMNS = ("todo", "doing", "done")
KANBAN_TITLES = {
    "todo": " TODO",
    "doing": " DOING",
    "done": " DONE",
    "blocked": " BLOCKED",
}


def render_kanban(
    entries: Iterable[BlackboardEntry],
    width: int = 28,
    show_blocked: bool = True,
) -> str:
    """
    Kanban

    Args:
        entries:  scope
        width:
        show_blocked:  BLOCKED
    """
    columns = list(KANBAN_COLUMNS)
    if show_blocked and any(e.status == "blocked" for e in entries):
        columns.append("blocked")

    #  status
    buckets = {col: [] for col in columns}
    other_count = 0
    for entry in entries:
        if entry.status in buckets:
            buckets[entry.status].append(entry)
        else:
            other_count += 1

    # updated_at
    for col in columns:
        buckets[col].sort(key=lambda e: e.updated_at, reverse=True)

    #
    sep = "+" + ("-" * width + "+") * len(columns)
    lines = [sep]

    #
    headers = []
    for col in columns:
        title = KANBAN_TITLES.get(col, col.upper())
        count = len(buckets[col])
        label = f" {title} ({count})"
        headers.append(label[:width].ljust(width))
    lines.append("|" + "|".join(headers) + "|")
    lines.append(sep)

    #
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
    """' topic (author)'  width"""
    label = f" {entry.topic} ({entry.author})"
    if len(label) > width:
        label = label[: width - 1] + ""
    return label.ljust(width)


def render_table(
    entries: List[BlackboardEntry],
    columns: tuple = ("id", "scope", "status", "topic", "author", "updated_at"),
    max_topic_len: int = 40,
) -> str:
    """ ASCII """
    if not entries:
        return "(no entries)"

    #
    widths = {col: len(col) for col in columns}
    for e in entries:
        for col in columns:
            value = _value(e, col, max_topic_len)
            widths[col] = max(widths[col], len(value))

    #
    def row(values):
        return " | ".join(v.ljust(widths[col]) for col, v in zip(columns, values))

    sep_line = "-+-".join("-" * widths[col] for col in columns)
    lines = [row(columns), sep_line]
    for e in entries:
        lines.append(row([_value(e, col, max_topic_len) for col in columns]))
    return "\n".join(lines)


def _value(entry: BlackboardEntry, col: str, max_topic_len: int) -> str:
    """ + """
    if col == "topic":
        val = entry.topic
        if len(val) > max_topic_len:
            val = val[: max_topic_len - 1] + ""
        return val
    if col == "updated_at":
        #  +
        return entry.updated_at[:16].replace("T", " ")
    if col == "id":
        return entry.id[:8]  #
    return str(getattr(entry, col, ""))
