"""
git_sync


- SyncConfig: hostname/username/git
- LogCollector:  +
- SkillLoader:
- CentralAggregator:  +  EvoLoop


1.   logs/{hostname}_{username}_{timestamp}.jsonl
2.   git checkout origin/main -- skills/
3.    sidechain/sync_audit.jsonl
4.    push memory/*.db / *.env /
"""

from .config import SyncConfig
from .log_collector import LogCollector, CollectResult
from .skill_loader import SkillLoader, ReloadResult
from .aggregator import CentralAggregator, AggregateReport

__all__ = [
    "SyncConfig",
    "LogCollector",
    "CollectResult",
    "SkillLoader",
    "ReloadResult",
    "CentralAggregator",
    "AggregateReport",
]
