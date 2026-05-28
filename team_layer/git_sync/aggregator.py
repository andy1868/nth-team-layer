"""
CentralAggregator  GitHub Action  23:00


1.  logs/*.jsonl ledger
2.  error_sig    ROI
3.  EvoLoop  Patch
4.  PR  Markdown


- sidechain/aggregated_ledger.jsonl   EvoLoop
- sidechain/aggregate_report.md       PR body
-  EvoLoop   AUTO_MERGE / PENDING_REVIEW
"""

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import SyncConfig


@dataclass
class AggregateReport:
    """PR body """
    total_entries: int = 0
    unique_hosts: int = 0
    error_sigs: Dict[str, int] = field(default_factory=dict)
    noisy_sigs_filtered: List[str] = field(default_factory=list)
    evolved_sigs: List[str] = field(default_factory=list)
    auto_merged: List[str] = field(default_factory=list)
    pending_review: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_markdown(self) -> str:
        """ PR-friendly """
        lines = [
            "#  Daily Evolution Review",
            "",
            f"_Generated: {self.generated_at}_",
            "",
            "##  ",
            "",
            f"- Total ledger entries collected: **{self.total_entries}**",
            f"- Unique hosts: **{self.unique_hosts}**",
            f"- Distinct error signatures: **{len(self.error_sigs)}**",
            f"- Noisy signatures filtered (low ROI): **{len(self.noisy_sigs_filtered)}**",
            f"- Signatures that triggered evolution: **{len(self.evolved_sigs)}**",
            "",
        ]

        if self.error_sigs:
            lines.append("##  Top ")
            lines.append("")
            lines.append("| Signature | Count |")
            lines.append("|---|---|")
            top = sorted(self.error_sigs.items(), key=lambda x: x[1], reverse=True)[:10]
            for sig, count in top:
                lines.append(f"| `{sig}` | {count} |")
            lines.append("")

        if self.auto_merged:
            lines.append("##  Auto-Merged Skills (low risk)")
            lines.append("")
            for sig in self.auto_merged:
                lines.append(f"- `{sig}`  `skills/registry/`")
            lines.append("")

        if self.pending_review:
            lines.append("##  Pending Review (high risk  needs approval)")
            lines.append("")
            for sig in self.pending_review:
                lines.append(f"- `{sig}`  `sidechain/pending_patches/`")
            lines.append("")

        if self.rejected:
            lines.append("##  Rejected (verify failed)")
            lines.append("")
            for sig in self.rejected:
                lines.append(f"- `{sig}`")
            lines.append("")

        if self.noisy_sigs_filtered:
            lines.append("<details><summary>Filtered low-ROI signatures</summary>")
            lines.append("")
            for sig in self.noisy_sigs_filtered[:20]:
                lines.append(f"- `{sig}`")
            if len(self.noisy_sigs_filtered) > 20:
                lines.append(f"-  and {len(self.noisy_sigs_filtered) - 20} more")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("**Action**: Merge to apply auto-merged skills team-wide. ")
        lines.append("Review pending patches manually before merging high-risk fixes.")
        return "\n".join(lines)


class CentralAggregator:
    """"""

    def __init__(
        self,
        config: Optional[SyncConfig] = None,
        noise_min_count: int = 2,  #
    ):
        self.cfg = config or SyncConfig()
        self.noise_min_count = noise_min_count

    def run(self, trigger_evolution: bool = True) -> AggregateReport:
        """  """
        report = AggregateReport()

        # Phase 1:  logs/*.jsonl
        merged_entries, host_set = self._merge_logs()
        report.total_entries = len(merged_entries)
        report.unique_hosts = len(host_set)
        print(f"[AGGREGATE] Merged {report.total_entries} entries from {report.unique_hosts} host(s)")

        # Phase 2: EvoLoop
        agg_ledger = self.cfg.sidechain_path() / "aggregated_ledger.jsonl"
        agg_ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(agg_ledger, "w", encoding="utf-8") as f:
            for entry in merged_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[AGGREGATE] Wrote aggregated ledger  {agg_ledger.name}")

        # Phase 3:  +
        sig_counts = defaultdict(int)
        for entry in merged_entries:
            sig = entry.get("error_sig")
            if sig:
                sig_counts[sig] += 1
        report.error_sigs = dict(sig_counts)
        report.noisy_sigs_filtered = [
            sig for sig, cnt in sig_counts.items() if cnt < self.noise_min_count
        ]
        print(
            f"[AGGREGATE] {len(sig_counts)} sigs, "
            f"{len(report.noisy_sigs_filtered)} filtered as noise (count < {self.noise_min_count})"
        )

        # Phase 4:  EvoLoop
        if trigger_evolution:
            self._run_evolution(agg_ledger, report)

        # Phase 5:  PR
        report_path = self.cfg.sidechain_path() / "aggregate_report.md"
        report_path.write_text(report.to_markdown(), encoding="utf-8")
        print(f"[AGGREGATE] Wrote PR report  {report_path.name}")

        return report

    #

    def _merge_logs(self) -> Tuple[List[dict], set]:
        """ logs/*.jsonl  +  +  timestamp """
        logs_dir = self.cfg.logs_path()
        if not logs_dir.exists():
            return [], set()

        entries = []
        seen_keys = set()
        hosts = set()

        for log_file in sorted(logs_dir.glob("*.jsonl")):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        #  key: (timestamp, agent_id, action_type, result-prefix)
                        key = (
                            entry.get("timestamp"),
                            entry.get("agent_id"),
                            entry.get("action_type"),
                            str(entry.get("result", ""))[:30],
                        )
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        entries.append(entry)

                        host = (entry.get("collected_by") or {}).get("hostname")
                        if host:
                            hosts.add(host)
            except Exception as e:
                print(f"[AGGREGATE] Failed to read {log_file.name}: {e}")

        #  timestamp
        entries.sort(key=lambda e: e.get("timestamp", ""))
        return entries, hosts

    def _run_evolution(self, agg_ledger_path: Path, report: AggregateReport) -> None:
        """ EvoLoop"""
        from ..memory_providers import LedgerProvider
        from ..evolution import EvoLoop, EvoTrigger

        #  LedgerProvider
        ledger = LedgerProvider(str(agg_ledger_path))
        ledger.initialize({})

        trigger = EvoTrigger(ledger)
        loop = EvoLoop(ledger=ledger, trigger=trigger)
        results = loop.run_once()

        for result in results:
            sig = result.decision.error_sig
            report.evolved_sigs.append(sig)
            if result.gate:
                action = result.gate.action.value
                if action == "auto_merge":
                    report.auto_merged.append(sig)
                elif action == "pending_review":
                    report.pending_review.append(sig)
                elif action == "rejected":
                    report.rejected.append(sig)

        print(
            f"[AGGREGATE] EvoLoop done: "
            f"{len(report.auto_merged)} merged, "
            f"{len(report.pending_review)} pending, "
            f"{len(report.rejected)} rejected"
        )
