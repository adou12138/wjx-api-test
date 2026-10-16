# -*- coding: utf-8 -*-
"""加载测试环境替身（单一来源见 _bootstrap.py）。"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
