"""轻量测试 runner：环境装不了 pytest 时用 ``python3 tests/run_tests.py``。

它只实现 pytest 风格测试真正用到的极小特性：``test_`` 函数发现、
``assert`` 断言、``pytest.raises`` 与模块级 ``setup`` 不需要 fixture
（测试通过普通工厂函数构造对象）。装了 pytest 时 ``python3 -m pytest``
仍是首选入口。
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path


class _Raises:
    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.value: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"预期抛出 {self.exc_type.__name__}，但没有抛出")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        return True


class _MiniPytest:
    raises = staticmethod(_Raises)
    mark = type("mark", (), {"skip": staticmethod(lambda *a, **k: None)})()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    pytest_shim = _MiniPytest()
    sys.modules.setdefault("pytest", pytest_shim)  # 让 import pytest 也能跑

    test_files = sorted(p for p in Path(__file__).parent.glob("test_craft*.py"))
    total = failures = 0
    for path in test_files:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        names = [n for n in dir(module) if n.startswith("test_")]
        for name in names:
            total += 1
            try:
                getattr(module, name)()
            except Exception:  # noqa: BLE001 - runner 要收集每个失败
                failures += 1
                print(f"FAIL {path.name}::{name}")
                traceback.print_exc()
            else:
                print(f"PASS {path.name}::{name}")
    print(f"\n{total - failures}/{total} 通过，{failures} 失败")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
