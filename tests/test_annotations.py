"""타입 어노테이션에 쓰인 이름이 실제로 import 되어 있는지 검사한다.

개발 환경은 Python 3.14 라 어노테이션이 지연 평가된다(PEP 649). 그래서 어노테이션에만
쓰이는 이름을 import 에서 빠뜨려도 **여기서는 아무 일도 안 일어난다** — 그런데
pyproject 가 지원한다고 적어 둔 3.10~3.13 에서는 모듈 import 자체가 NameError 로
실패한다. 랩 PC 파이썬이 더 낮을 수 있으므로 강제로 평가해 확인한다.

실제로 이 검사로 mfli_merge 의 Optional 누락을 잡았다 (미사용 import 정리 과정에서
어노테이션 전용 이름이 지워졌다).
"""
import importlib
import inspect
import pkgutil
import unittest

PKG = "pythonization"


def _iter_modules():
    package = importlib.import_module(PKG)
    yield package
    for _finder, name, _ispkg in pkgutil.walk_packages(package.__path__,
                                                       prefix=f"{PKG}."):
        yield importlib.import_module(name)


class TestAnnotationsResolve(unittest.TestCase):
    def test_all_annotations_evaluate(self):
        problems = []
        checked = 0

        for module in _iter_modules():
            targets = [module]
            for _name, obj in vars(module).items():
                if inspect.isclass(obj) or inspect.isfunction(obj):
                    if getattr(obj, "__module__", None) == module.__name__:
                        targets.append(obj)
                        if inspect.isclass(obj):
                            targets.extend(
                                m for _n, m in vars(obj).items()
                                if inspect.isfunction(m))

            for target in targets:
                try:
                    annotations = getattr(target, "__annotations__", None)
                except NameError as exc:
                    problems.append(f"{module.__name__}.{getattr(target, '__qualname__', '?')}"
                                    f": {exc}")
                    continue
                if annotations:
                    checked += len(annotations)

        self.assertEqual(
            [], problems,
            "어노테이션이 해석되지 않는다 (Python 3.13 이하에서 import 실패):\n  "
            + "\n  ".join(problems))
        self.assertGreater(checked, 100,
                           "평가된 어노테이션이 너무 적다 — 탐색 범위를 확인할 것")


class TestConsoleSafePrints(unittest.TestCase):
    """print() 로 나가는 문자열이 cp949 콘솔에서 죽지 않는지.

    랩 PC 는 한국어 Windows 라 콘솔 코드페이지가 cp949 다. `pythonization.bat` 은
    콘솔에서 python main.py 를 돌리므로, print 에 cp949 에 없는 문자(예: em-dash
    U+2014, U+2713)가 있으면 그 줄에서 UnicodeEncodeError 가 나고 **경고 하나 때문에
    그 경로 전체가 죽는다**. 실제로 VNA 설정 저장 경로에 그런 print 가 두 개 있었다.

    사용자에게 보여 줄 진단 메시지는 print 대신 logging 을 쓴다 (app.log 는 utf-8).
    """

    def test_no_print_literal_breaks_cp949(self):
        import ast
        from pathlib import Path

        def encodable(ch: str) -> bool:
            try:
                ch.encode("cp949")
                return True
            except UnicodeEncodeError:
                return False

        pkg_root = Path(__file__).resolve().parent.parent / "pythonization"
        problems = []
        for path in sorted(pkg_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                is_print = (isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name)
                            and node.func.id == "print")
                if not is_print:
                    continue
                for sub in ast.walk(node):
                    if not (isinstance(sub, ast.Constant) and isinstance(sub.value, str)):
                        continue
                    bad = sorted({c for c in sub.value if not encodable(c)})
                    if bad:
                        problems.append(
                            f"{path.relative_to(pkg_root.parent)}:{node.lineno} "
                            f"{bad} in {sub.value[:40]!r}")

        self.assertEqual(
            [], problems,
            "cp949 콘솔에서 UnicodeEncodeError 를 낼 print:\n  " + "\n  ".join(problems))

if __name__ == "__main__":
    unittest.main()
