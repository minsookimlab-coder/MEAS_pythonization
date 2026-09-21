"""GlowFrame: 측정 중 테두리가 숨쉬듯 빛나는 컨테이너 위젯.

왜 따로 있나 — 예전에는 테두리 색을 `setStyleSheet()` 로 바꿨다. 그런데 이
위젯은 창의 내용 전체를 담는 컨테이너라, 스타일시트를 다시 지정하면 Qt 가
**이 위젯과 모든 자식**(메인 창 기준 369개)의 스타일을 다시 계산하고 polish 한다.
30 ms 주기로 도는 애니메이션이었으므로 측정 내내 GUI 스레드를 갉아먹었다.

실측 (메인 창, offscreen):

    setStyleSheet 방식   갱신 7.55 ms + 리페인트까지 12.89 ms  /  30 ms 마다
    이 위젯             갱신 0.006 ms + 리페인트까지  0.33 ms  /  30 ms 마다

스텝 처리와 애니메이션 틱이 겹칠 때만 지연이 드러나서 '간헐적'으로 보였다.

두 가지로 해결한다.
  1. 스타일시트는 처음 한 번만 (투명 테두리로 여백 확보). 색은 paintEvent 가 그린다.
  2. 갱신 시 위젯 전체가 아니라 **테두리 띠만** 무효화한다. 인자 없는 update() 는
     자식 수백 개까지 다시 그리게 만든다.
"""
from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QRegion
from PySide6.QtWidgets import QFrame


class GlowFrame(QFrame):

    _BORDER_W = 3
    _RADIUS = 6

    def __init__(self, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        # 여백 확보용 — 측정 중에는 다시 지정하지 않는다
        self.setStyleSheet(
            f"QFrame#{object_name} {{ border: {self._BORDER_W}px solid transparent;"
            f" border-radius: {self._RADIUS}px; }}"
        )
        self._glow_color: Optional[QColor] = None

    def set_glow(self, color: Optional[QColor]) -> None:
        """테두리 색 지정. None 이면 빛을 끈다. 같은 색이면 아무 일도 하지 않는다."""
        if color == self._glow_color:
            return
        self._glow_color = color
        self.update(self._border_region())

    def _border_region(self) -> QRegion:
        pad = self._BORDER_W + 2          # 안티앨리어싱 여유
        outer = self.rect()
        return QRegion(outer).subtracted(
            QRegion(outer.adjusted(pad, pad, -pad, -pad)))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._glow_color is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._glow_color, self._BORDER_W))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half = self._BORDER_W / 2.0
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(half, half, -half, -half),
            self._RADIUS, self._RADIUS)
        painter.end()


def glow_color(phase: float) -> QColor:
    """애니메이션 위상 → 테두리 색 (기존 sin 기반 초록 펄스 그대로)."""
    import math
    intensity = (math.sin(phase) + 1) / 2
    return QColor(40, int(140 + intensity * 80), 70, int(80 + intensity * 140))
