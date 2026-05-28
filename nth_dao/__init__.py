"""NTH DAO public API.

`nth_dao` is the forward-looking import path for the NTH DAO project. The
current implementation is still provided by `nth_team_layer` for backward
compatibility while the project migrates from Team Layer branding to DAO
protocol branding.
"""

from __future__ import annotations

import importlib
import sys

from nth_team_layer import *  # noqa: F401,F403
from nth_team_layer import __all__, __version__


_SUBMODULES = [
    "attach",
    "channel",
    "discovery",
    "gossip",
    "groups",
    "identity",
    "marketplace",
    "membership",
    "orchestration",
    "reputation",
]

for _name in _SUBMODULES:
    try:
        sys.modules[f"{__name__}.{_name}"] = importlib.import_module(
            f"nth_team_layer.{_name}"
        )
    except ImportError:
        pass

