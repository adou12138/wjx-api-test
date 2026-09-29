# -*- coding: utf-8 -*-
"""输入资产登记，避免低可信来源被静默当作硬契约。"""
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Asset:
    kind: str
    source: str
    trust: str = "medium"
    can_be_hard_assertion: bool = False
    version: Optional[str] = None
    note: str = ""

    def to_dict(self):
        return asdict(self)


def register(kind, source, trust="medium", version=None, note=""):
    return Asset(kind, source, trust, trust in ("high", "official"), version, note)
