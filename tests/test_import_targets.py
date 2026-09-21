"""소스에 적힌 모든 import 문이 실제로 해석되는지 정적으로 검사한다.

test_imports 는 '모듈을 import 할 수 있는가'만 본다. 그런데 이 코드베이스에는
함수 안에 숨은 import 가 많아서, 해당 메뉴를 클릭해야만 ImportError 가 터진다
(리팩터링 중 가장 놓치기 쉬운 회귀).

그래서 여기서는 ast 로 **중첩 import 까지 전부** 뽑아, 모듈이 존재하고 거기서
가져오려는 이름도 실제로 있는지 확인한다. GUI 를 띄우지 않고 검증된다.
"""
import ast
import importlib
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent / "pythonization"
PKG_NAME = "pythonization"


def _iter_import_nodes(tree):
    """모듈 최상단뿐 아니라 함수/메서드 안의 import 도 전부 훑는다."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def _module_of(path: Path) -> str:
    rel = path.relative_to(PKG_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([PKG_NAME, *parts])


class TestImportTargetsResolve(unittest.TestCase):
    def test_all_import_statements_resolve(self):
        problems = []
        checked = 0

        for path in sorted(PKG_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            here = _module_of(path)

            for node in _iter_import_nodes(tree):
                if isinstance(node, ast.Import):
                    targets = [(a.name, None) for a in node.names]
                else:
                    if node.level:      # 상대 import — 이 코드베이스는 쓰지 않는다
                        continue
                    if node.module is None:
                        continue
                    targets = [(node.module, a.name) for a in node.names]

                for module_name, symbol in targets:
                    # 이 프로젝트 모듈만 검사한다 (서드파티는 test_imports 가 커버).
                    if not module_name.startswith(PKG_NAME):
                        continue
                    checked += 1
                    try:
                        mod = importlib.import_module(module_name)
                    except Exception as exc:  # noqa: BLE001
                        problems.append(
                            f"{here}:{node.lineno} → 모듈 없음 '{module_name}' "
                            f"({type(exc).__name__}: {exc})")
                        continue
                    if symbol in (None, "*"):
                        continue
                    if hasattr(mod, symbol):
                        continue
                    # 서브패키지를 import 하는 형태(from pkg import subpkg)도 허용
                    try:
                        importlib.import_module(f"{module_name}.{symbol}")
                    except Exception:
                        problems.append(
                            f"{here}:{node.lineno} → '{module_name}' 에 "
                            f"'{symbol}' 이(가) 없음")

        self.assertEqual(
            [], problems,
            f"해석되지 않는 import {len(problems)}건:\n  " + "\n  ".join(problems),
        )
        self.assertGreater(checked, 100, "검사된 import 가 너무 적다 — 탐색 경로 확인")


if __name__ == "__main__":
    unittest.main()
