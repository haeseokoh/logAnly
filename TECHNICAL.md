# logAnly 기술 구현 정리

## 1. 목적과 설계 원칙

이 프로젝트는 Android `adb bugreport` 대용량 ZIP을 설치 없이 분석하기 위한 로컬 RAG 도구입니다. 핵심 구현은 `real-bugreports/bugreport_index.py`이며 Python 표준 라이브러리만 사용합니다.

주요 원칙은 다음과 같습니다.

- 원본 ZIP을 수정하거나 압축 해제하지 않고 ZIP 스트림에서 직접 읽습니다.
- 주 보고서 전체를 메모리에 올리지 않고 한 줄씩 처리합니다.
- 검색과 LLM 답변에서 항상 ZIP 내부 출처와 원본 줄 범위를 보존합니다.
- 기존 FTS DB와 임베딩 DB를 분리해 재인덱싱 중 기존 결과를 보호합니다.
- Ollama 모델을 자동 다운로드하거나 임의의 대체 모델로 변경하지 않습니다.
- 모든 LLM 처리와 임베딩 처리는 로컬 Ollama HTTP API에서 수행합니다.

## 2. 전체 처리 구조

```text
bugreport ZIP
  └─ main_entry.txt
       └─ 주 보고서 엔트리
            └─ 줄 단위 스트리밍
                 └─ 섹션/로그/스택 청크
                      ├─ SQLite chunks
                      ├─ SQLite FTS5
                      └─ embeddinggemma 벡터 BLOB

질문
  ├─ FTS5/BM25 키워드 검색
  ├─ embeddinggemma 질의 임베딩 + 코사인 검색
  └─ RRF 순위 결합
       └─ 적중 청크 + 인접 청크
            └─ 출처 포함 근거 프롬프트
                 └─ gemma4:e4b 한국어 답변
```

## 3. ZIP과 주 보고서 탐색

`zipfile.ZipFile`로 ZIP 중앙 디렉터리만 읽고 `main_entry.txt`를 찾습니다. `main_entry.txt`의 첫 내용을 UTF-8로 해석해 실제 주 보고서 엔트리 이름으로 사용합니다.

엔트리가 ZIP 루트가 아닌 하위 경로에 있는 경우에도 suffix가 유일하면 찾을 수 있습니다. 대상이 없거나 모호하면 원본을 건드리지 않고 오류로 종료합니다.

주 보고서는 `ZipFile.open()`이 반환하는 바이너리 스트림을 순회합니다. 각 줄만 UTF-8로 디코딩하며 손상된 문자는 replacement character로 대체합니다. 따라서 비압축 크기가 수십 MB 이상이어도 보고서 전체 크기에 비례하는 메모리가 필요하지 않습니다.

## 4. 청크화와 메타데이터

청크 경계는 Android bugreport의 실제 텍스트 형식을 정규식으로 감지합니다.

- 최상위 섹션: `------ SYSTEM LOG (...) ------`
- logcat 버퍼: `--------- beginning of crash`
- threadtime 로그: 시간, PID, TID, 심각도, 태그
- Java 스택: `FATAL EXCEPTION`, `java.lang.*`, `Caused by:`, `at ...`
- native 스택: `#00 pc ...`, `backtrace`, `stack trace`

일반 청크의 기본 상한은 200줄입니다. 스택 청크는 프레임 연속성을 우선하면서도 일반 상한의 2배를 넘지 않도록 제한합니다. 이 방식은 로그 한 건과 스택을 최대한 함께 보존하면서 비정상적으로 큰 단일 청크를 방지합니다.

각 청크에는 다음 메타데이터가 저장됩니다.

- 보고서 ID와 보고서 내 순서 `seq`
- 섹션 이름과 ZIP 내부 주 보고서 경로
- 원본 시작 줄과 끝 줄
- 청크 종류: `section`, `log`, `stack`, `text`
- 첫/마지막 감지 시간
- 태그 또는 프로세스 목록
- PID 목록
- 청크에서 감지한 최고 로그 심각도
- 스택 트레이스 여부
- 원문 내용

## 5. SQLite와 FTS5

기본 인덱스 파일은 `bugreports.sqlite3`입니다.

### 주요 테이블

`reports`

- ZIP의 절대 경로, 이름, 크기, 수정 시각
- `main_entry.txt`가 가리킨 엔트리와 비압축 크기
- 인덱싱 시각과 청크 수

`chunks`

- 청크 내용과 모든 검색/출처 메타데이터
- `(report_id, seq)` 고유 제약
- 보고서와 인접 청크를 빠르게 찾기 위한 인덱스

`chunks_fts`

- `content`, `section`, `process`를 대상으로 하는 FTS5 가상 테이블
- FTS5가 포함된 Python SQLite에서는 BM25 순위 검색 사용
- FTS5가 없는 환경에서는 `LIKE` 검색으로 안전하게 폴백

`meta`

- 스키마 버전, FTS5 활성 여부, 생성 시각
- 하이브리드 DB에서는 임베딩 모델, 차원, 행 수, 생성 시각 추가

SQLite는 WAL과 `synchronous=NORMAL` 설정으로 구축 성능과 안정성을 절충합니다. 새 인덱스는 임시 DB에 완성한 후 `os.replace()`로 원자 교체합니다. Windows 파일 잠금을 피하기 위해 교체 전에 SQLite 연결을 명시적으로 닫습니다.

## 6. embeddinggemma 임베딩

임베딩 기본 모델은 Ollama의 `embeddinggemma:latest`입니다. `/api/tags`에서 설치 여부를 확인한 뒤 `/api/embed`를 호출합니다.

```json
{
  "model": "embeddinggemma:latest",
  "input": ["search_document: ...", "search_document: ..."]
}
```

대량 처리 시 기본 8개, 설정 가능한 최대 64개 청크만 한 배치에 포함합니다. 각 청크의 임베딩 입력 문자 수도 제한합니다. 실제 검증에서는 16개 배치와 청크당 4,000자를 사용했습니다.

기존 DB 보호를 위해 `embed-index`는 SQLite backup API로 원본 FTS DB를 새 임시 DB에 복사합니다. 임베딩이 모두 완성된 후에만 최종 하이브리드 DB로 교체합니다. 입력 DB와 출력 DB가 같으면 즉시 거부합니다.

### 벡터 저장 형식

`embeddings` 테이블은 다음 데이터를 저장합니다.

- `chunk_id`: `chunks.id`와 연결되는 기본 키
- `model`: 임베딩 생성 모델
- `dimensions`: 벡터 차원
- `norm`: 정규화 전 L2 norm
- `content_chars`: 임베딩 입력 문자 수
- `vector`: L2 정규화된 little-endian float32 BLOB

실제 `embeddinggemma` 결과는 768차원입니다. 한 벡터 BLOB은 `768 × 4 = 3,072 bytes`입니다. Python `array('f')`를 사용하므로 NumPy 같은 외부 패키지가 필요하지 않습니다.

## 7. 하이브리드 검색

하이브리드 검색은 두 검색 결과를 결합합니다.

1. FTS5에서 질문 토큰의 OR 검색을 수행하고 BM25 상위 후보를 구합니다.
2. 질문에 `search_query:` 접두사를 붙여 `embeddinggemma`로 임베딩합니다.
3. SQLite BLOB을 순차적으로 읽어 모든 청크와 코사인 유사도를 계산합니다.
4. FTS 순위와 벡터 순위를 Reciprocal Rank Fusion으로 합칩니다.

RRF 점수는 각 목록에서 다음 값을 더합니다.

```text
score(chunk) += 1 / (60 + rank)
```

이 방식은 BM25와 코사인 점수의 서로 다른 수치 범위를 직접 정규화하지 않아도 두 순위를 안정적으로 결합합니다. 현재 구현은 외부 벡터 DB 없이 전체 벡터를 순차 스캔합니다. 검증 규모인 8,708개×768차원에서는 실용적이지만 수십만 청크 이상에서는 ANN 인덱스 또는 벡터 확장을 검토할 수 있습니다.

기존 `search` 명령은 Ollama와 무관한 순수 FTS 검색으로 계속 유지됩니다.

## 8. RAG와 Ollama 답변 생성

생성 기본 모델은 `gemma4:e4b`이며 임베딩 모델과 역할이 분리됩니다.

- `embeddinggemma:latest`: 문서 청크 및 질문을 벡터화해 검색에 사용
- `gemma4:e4b`: 검색된 근거를 읽고 한국어 분석 답변 생성

`ask` 처리 순서는 다음과 같습니다.

1. `/api/tags`로 생성 모델 설치 여부 확인
2. 하이브리드 DB이면 질문 임베딩과 결합 검색 수행
3. 각 적중 청크의 앞뒤 `seq` 청크 확장
4. 전체 문자 예산과 청크별 문자 예산 적용
5. 각 근거에 `[S1]` 라벨과 `ZIP::내부파일:시작줄-끝줄` 부여
6. `/api/chat`을 `stream: false`, 낮은 temperature로 호출
7. 모델 답변 뒤에 도구가 실제 근거 출처 목록을 다시 출력

시스템 프롬프트는 제공된 근거만 사용하고, 근거 부족을 명시하며, 확정 사실과 원인 후보를 구분하도록 요구합니다. 모델이 인용을 누락하더라도 도구가 마지막에 사용된 전체 근거 목록을 결정적으로 출력합니다.

## 9. 안전한 실패 처리

다음 상황에서는 한국어 오류와 종료 코드 2로 중단합니다.

- Ollama 서비스에 연결할 수 없음
- 설치된 모델이 없음
- 기본 또는 사용자가 지정한 생성 모델이 설치되지 않음
- 지정한 임베딩 모델이 설치되지 않음
- DB를 만든 임베딩 모델과 질의 모델이 다름
- `/api/embed` 응답 개수나 차원이 잘못됨
- 비정상 또는 0 norm 벡터 수신
- 기존 DB와 임베딩 출력 DB 경로가 같음
- 손상 ZIP 또는 잘못된 `main_entry.txt`

모델 다운로드 명령은 코드에 없으며 설치되지 않은 모델을 다른 모델로 자동 대체하지 않습니다.

## 10. 주요 CLI

```powershell
# ZIP 3개를 스트리밍 FTS 인덱싱
python .\real-bugreports\bugreport_index.py index .\real-bugreports --db .\real-bugreports\bugreports.sqlite3

# 기존 FTS 검색
python .\real-bugreports\bugreport_index.py search --db .\real-bugreports\bugreports.sqlite3 "FATAL EXCEPTION" --context 1

# 기존 DB를 보존하며 임베딩 DB 생성
python .\real-bugreports\bugreport_index.py embed-index --db .\real-bugreports\bugreports.sqlite3 --output .\real-bugreports\bugreports-hybrid.sqlite3

# FTS + 벡터 하이브리드 검색
python .\real-bugreports\bugreport_index.py hybrid-search --db .\real-bugreports\bugreports-hybrid.sqlite3 "런처 권한 문제"

# 하이브리드 검색 근거로 한국어 답변 생성
python .\real-bugreports\bugreport_index.py ask --db .\real-bugreports\bugreports-hybrid.sqlite3 "FATAL EXCEPTION 원인을 설명해 줘"
```

## 11. 실제 검증 수치

- bugreport ZIP: 3개
- 주 보고서 비압축 합계: 120,927,539 bytes
- 생성 청크: 8,708개
- 스택 청크: 328개
- 임베딩: 8,708개, 누락 0
- 임베딩 차원: 768
- 벡터 BLOB: 행당 3,072 bytes
- FTS DB: 165,695,488 bytes
- 하이브리드 DB: 199,565,312 bytes
- SQLite `integrity_check`: `ok`
- 단위 테스트: 5개 통과
- 원본 ZIP 및 기존 FTS DB: 처리 전후 SHA-256 동일

세부 실행 결과는 다음 파일에 있습니다.

- `real-bugreports/VALIDATION.md`
- `real-bugreports/validation-output.txt`
- `real-bugreports/ollama-validation-output.txt`
- `real-bugreports/embedding-validation-output.txt`

## 12. 표준 라이브러리 사용 범위

- `zipfile`: ZIP 엔트리 스트리밍
- `sqlite3`: 관계형 메타데이터, FTS5, 트랜잭션, DB backup
- `urllib.request`: Ollama `/api/tags`, `/api/embed`, `/api/chat`
- `array`: float32 벡터 BLOB 변환
- `math`: L2 norm과 벡터 검증
- `re`: 섹션, logcat, 시간, PID, 심각도, 스택 패턴 추출
- `argparse`: CLI 하위 명령과 옵션
- `pathlib`, `os`: 안전한 경로 및 원자적 파일 교체
