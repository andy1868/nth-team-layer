"""
git_sync — 多终端协同子系统

组件：
- SyncConfig: 跨平台配置（hostname/username/git 路径）
- LogCollector: 本地日志采集 + 推送
- SkillLoader: 原子级技能热加载
- CentralAggregator: 跨终端日志合并 + 触发 EvoLoop

设计原则（来自原始团队协同规范）：
1. 零冲突命名 — logs/{hostname}_{username}_{timestamp}.jsonl
2. 原子级热加载 — git checkout origin/main -- skills/，不动工作区
3. 审计先行 — 所有同步操作写入 sidechain/sync_audit.jsonl
4. 安全默认 — 永不 push memory/*.db / *.env / 凭据文件
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
