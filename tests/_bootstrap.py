# -*- coding: utf-8 -*-
"""测试环境替身引导（单一来源）。

干净克隆上 WJX_BASE_URL 等未配置时，api_test_runner.BASE_URL=''，
`urllib.request.Request("")` 在进入 urlopen **之前**就抛 ValueError——
mock.patch("urllib.request.urlopen") 拦不到，网络 mock 用例必红。

本模块在任何生产代码 import 之前 setdefault 注入占位值。三个入口都引它：
- tests/conftest.py（pytest 自动加载）
- tests/__init__.py（`unittest discover -s tests -t .` 按包导入时）
- 各测试文件可在 sys.path 注入后 `import _bootstrap`（顶层导入兜底）

占位值用 RFC 2606 保留 TLD（.invalid）：即便某测试漏了 mock，请求也只是
code=-1（归 ENV），绝不会发往真实服务器。只在用户未显式配置时生效，
不影响本机真实回归。修改占位值时**只改这里**，两处入口自动同步。
"""
import os

for _k, _v in {
    "WJX_BASE_URL": "https://example.invalid/api",
    "WJX_APPID": "test_appid",
    "WJX_MAIN": "test_main_account",
    "WJX_SUB": "test_sub_account",
    "WJX_CORPID": "test_corpid",
}.items():
    os.environ.setdefault(_k, _v)
