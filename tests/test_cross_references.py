"""다른 클래스의 내부 속성을 꺼내 쓰는 참조가 실제로 존재하는지 검사한다.

Double Sweep / VNA 창은 MainWindow 의 내부(`mw._deriv_channel`,
`self._main_win._meta_manager` …)를 직접 쓴다. MainWindow 쪽에서 이름을 바꾸거나
지우면 **측정을 실제로 돌려야만** AttributeError 로 드러난다 — import 검사도,
창 생성 검사도 못 잡는다.

이 테스트가 있는 이유: 리팩터링 중 MainWindow 의 `_deriv_val_for_step/2/3` 래퍼를
지웠는데 double_sweep 이 그걸 부르고 있었다. 테스트 120여 개가 전부 통과한 상태로
Double Sweep 첫 스텝에서 죽는 코드가 커밋될 뻔했다.
"""
import ast
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication          # noqa: E402

from pythonization.ui.main_window import MainWindow  # noqa: E402

_app = QApplication.instance() or QApplication([])

PKG_ROOT = Path(__file__).resolve().parent.parent / "pythonization"

#: 다른 창을 가리키는 변수 이름 → 그 변수가 담고 있는 클래스
HOLDER_CLASSES = {
    "mw": MainWindow,
    "_main_win": MainWindow,
    "main_win": MainWindow,
}


def _attribute_refs(path: Path):
    """`mw._attr` / `self._main_win._attr` 형태의 참조를 뽑는다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        holder = None
        if isinstance(base, ast.Name):
            holder = HOLDER_CLASSES.get(base.id)
        elif isinstance(base, ast.Attribute):
            holder = HOLDER_CLASSES.get(base.attr)
        if holder is not None:
            yield holder, node.attr, node.lineno


def _assigned_instance_attrs(path: Path) -> set:
    """소스에서 `self.x = ...` / `self.x: T = ...` 로 대입되는 이름들.

    메서드는 클래스에서 바로 보이지만 인스턴스 속성은 안 보이므로 따로 모은다.
    '대입'만 봐야 한다 — `self.x` 등장 여부로 판단하면 호출(`self.x()`)까지 걸려
    이름이 사라진 것을 놓친다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.add(target.attr)
    return names


class TestCrossClassReferences(unittest.TestCase):
    def test_referenced_attributes_exist(self):
        instance_attrs = _assigned_instance_attrs(PKG_ROOT / "ui" / "main_window.py")
        problems = []
        checked = 0

        for path in sorted(PKG_ROOT.rglob("*.py")):
            for holder, attr, lineno in _attribute_refs(path):
                checked += 1
                if hasattr(holder, attr):
                    continue        # 메서드·프로퍼티
                if attr in instance_attrs:
                    continue        # __init__ 등에서 대입되는 인스턴스 속성
                problems.append(
                    f"{path.relative_to(PKG_ROOT.parent)}:{lineno} → "
                    f"{holder.__name__}.{attr} 가 없다")

        self.assertEqual(
            [], problems,
            f"깨진 교차 참조 {len(problems)}건:\n  " + "\n  ".join(problems))
        self.assertGreater(checked, 50,
                           "검사된 참조가 너무 적다 — 탐색 규칙을 확인할 것")


if __name__ == "__main__":
    unittest.main()
