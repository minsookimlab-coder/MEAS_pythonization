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


if __name__ == "__main__":
    unittest.main()
