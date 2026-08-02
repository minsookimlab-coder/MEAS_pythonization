"""모든 모듈이 import 되는지 검사한다.

리팩터링(파일 이동·import 경로 재작성) 중 가장 흔한 회귀는 '어떤 창을 열면
ModuleNotFoundError'다. GUI를 띄우지 않고도 잡아내려면 전 모듈을 실제로
import 해보는 수밖에 없다.

기준선(리팩터링 착수 시점, e1a0fae): 57개 모듈 import 성공 / 실패 0.
이 수는 파일을 쪼개면 늘어난다. 줄어들면 뭔가 사라진 것이므로 확인할 것.
"""
import importlib
import pkgutil
import unittest

# 검사 대상 최상위 패키지. 레이아웃이 바뀌면 여기만 고치면 된다.
ROOT_PACKAGES = ("config", "core", "driver", "gui")


def _iter_modules(package_name: str):
    """패키지 하위의 모든 모듈 이름을 재귀적으로 훑는다."""
    pkg = importlib.import_module(package_name)
    yield package_name
    for _finder, name, _ispkg in pkgutil.walk_packages(
        pkg.__path__, prefix=f"{package_name}."
    ):
        yield name


class TestAllModulesImport(unittest.TestCase):
    def test_every_module_imports(self):
        failures = []
        count = 0
        for root in ROOT_PACKAGES:
            for mod in sorted(_iter_modules(root)):
                count += 1
                try:
                    importlib.import_module(mod)
                except Exception as exc:  # noqa: BLE001 - 어떤 실패든 보고해야 한다
                    failures.append(f"{mod}: {type(exc).__name__}: {exc}")

        self.assertEqual(
            [], failures,
            "다음 모듈이 import 되지 않는다:\n  " + "\n  ".join(failures),
        )
        # 모듈이 통째로 사라지는 실수(패키지 __init__ 누락 등)를 잡는 하한선.
        self.assertGreaterEqual(
            count, 57,
            f"발견된 모듈이 {count}개뿐이다 — 기준선 57개보다 적다. "
            "패키지 __init__.py 누락이나 파일 유실을 의심할 것.",
        )


if __name__ == "__main__":
    unittest.main()
