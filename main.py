"""실행 진입점 — `python main.py` 로 앱을 띄운다.

실제 기동 순서는 pythonization/app/bootstrap.py 에 있다. 이 파일은 프로젝트
루트를 import 경로에 넣어 주는 얇은 셸이라, 가상환경에 설치하지 않고도
어느 랩 PC에서든 그대로 실행된다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pythonization.app.bootstrap import main  # noqa: E402  (경로 설정 후 import)

if __name__ == "__main__":
    sys.exit(main())
