"""강의 실습 스크립트가 공유하는 준비물.

프로젝트 루트를 import 경로에 넣고, 출력 DB를 한곳에 모으고,
윈도우 콘솔에서 한글이 깨지지 않게 stdout 인코딩을 맞춘다.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent / "_out"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# cp949 콘솔에서도 한글이 그대로 나오게 한다. PYTHONIOENCODING을 걸지 않아도 된다.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def fresh_db(name: str) -> Path:
    """`_out/<name>.db`를 비우고 경로를 돌려준다. WAL 부산물도 함께 지운다."""
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        sibling = Path(str(path) + suffix)
        if sibling.exists():
            sibling.unlink()
    return path


def line(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
