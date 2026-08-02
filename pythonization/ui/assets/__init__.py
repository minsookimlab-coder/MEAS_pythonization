"""GUI 정적 자산(이미지 등).

파일 경로를 직접 조립하지 말고 asset_path() 를 쓴다. PyInstaller 로 묶었을 때도
같은 코드가 동작하도록 이 폴더가 build_exe.spec 의 datas 에 포함되어 있다.
"""
from pathlib import Path

_DIR = Path(__file__).resolve().parent

#: Sweep Parameters 패널 배경에 반투명하게 깔리는 고래 그림
WHALE_BACKGROUND = "whale.png"


def asset_path(name: str) -> str:
    """자산 파일의 절대 경로. 파일이 없어도 예외를 내지 않는다.

    호출부(_WhaleBgFrame)가 QPixmap.isNull() 로 부재를 처리하므로, 자산이 빠져도
    장식이 사라질 뿐 측정 기능은 영향받지 않는다.
    """
    return str(_DIR / name)
