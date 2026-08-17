# 실습 스크립트

설계 문서에 적힌 주장을 실제로 돌려서 확인하는 스크립트들입니다. 각각 임시 DB를
하나 만들고, 문서를 넣고, 테이블 안을 들여다봅니다. 프로젝트의 `omnilog.db`는
건드리지 않습니다.

```bash
uv run python scripts/lecture01_layout.py
uv run python scripts/lecture02_04_storage.py
uv run python scripts/lecture05_search.py
uv run python scripts/lecture06_08_runtime.py
uv run python scripts/lecture09_sources.py
```

DB는 `scripts/_out/` 에 남으니 DB 브라우저로 열어봐도 됩니다. 실행할 때마다
새로 만들어집니다. `_out/`은 `.gitignore`에 들어 있습니다.

> 스크립트 디렉터리가 `sys.path`에 들어가야 `_common`을 찾습니다. 위처럼 파일
> 경로로 실행하면 됩니다. `python -m scripts.lecture01_layout` 형태로는 동작하지
> 않습니다.

| 스크립트 | 확인하는 것 |
|---|---|
| `lecture01_layout.py` | rename 전후 테이블 상태. 이동 리비전이 직전 `text_id`를 재사용해 본문이 복사되지 않는 것 |
| `lecture02_04_storage.py` | 리비전 61개를 만들고 압축. 재인코딩 후에도 같은 문자열이 나오는지, 델타 적용이 최대 1회인지, 조각을 나눠 압축하면 왜 커지는지 |
| `lecture05_search.py` | `fts5vocab`으로 실제 색인어를 꺼내 unicode61과 trigram 비교. BM25 제목 가중치, FTS5 연산자 주입, 인덱스 크기 |
| `lecture06_08_runtime.py` | 링크가 빨강→파랑→리다이렉트로 바뀔 때의 캐시 무효화, 편집 충돌 409, `BEGIN DEFERRED` vs `IMMEDIATE`, 요청당 연결 수명 |
| `lecture09_sources.py` | `citation` 행이 인라인 정의와 레지스트리 참조에서 각각 담는 것, 출처를 한 번 고치면 인용한 문서 전부가 바뀌는 것(`cite_state`), 끊어진 인용, 세 종류(link/text/file)의 저장 위치, 역인덱스 |

## 알아둘 것

**한글 출력** — `_common.py`가 `sys.stdout`을 UTF-8로 맞춰둬서 윈도우 콘솔에서도
깨지지 않습니다. `PYTHONIOENCODING`을 따로 걸 필요 없습니다.

**`lecture02_04_storage.py`는 좀 걸립니다** — 35KB짜리 본문으로 리비전 61개를
쓰고 압축까지 하므로 수십 초 걸립니다. `REVISIONS` 상수를 줄이면 빨라지는데,
17 미만으로 내리면 키프레임 구간이 하나뿐이라 블롭이 여러 개 생기는 모습을 볼 수
없습니다.

**`lecture06_08_runtime.py`의 동시성 부분** — 일부러 락 경합을 만들고 한쪽이
실패하는 것을 보여줍니다. 출력에 나오는 `database is locked`는 오류가 아니라
관찰 대상입니다.

**6강과 9강은 짝입니다** — 렌더 캐시에는 지문이 둘 있습니다. `link_state`는
`[[위키링크]]`가 가리키는 문서 쪽을, `cite_state`는 `[^@키]`가 가리키는 출처 쪽을
지켜봅니다. 6강이 첫 번째를, 9강이 두 번째를 같은 모양으로 보여줍니다.

**측정값은 환경을 탑니다** — 대기 시간(0.4초대), 파일 크기, gzip 결과 바이트 수는
SQLite 빌드와 zlib 버전에 따라 조금씩 다릅니다. 자릿수와 방향이 같으면 같은 현상을
본 것입니다.

## 얽혀 있는 문서

- [`README.md`](../README.md) — 저장 구조와 압축 설계
- [`GLOSSARY.md`](../GLOSSARY.md) — 용어집
