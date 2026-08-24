# Android bugreport ZIP 스트리밍 검색 도구

`bugreport_index.py`는 Android `adb bugreport` ZIP의 `main_entry.txt`가 가리키는 주 보고서를 한 줄씩 읽어 SQLite 검색 인덱스를 만듭니다. Python 표준 라이브러리만 사용하므로 별도 설치가 필요 없습니다. ZIP 전체나 주 보고서 전체를 메모리에 올리지 않으며 원본 ZIP을 수정하지 않습니다.

## 요구 사항과 설치

- Python 3.10 이상 권장 (`python --version`으로 확인)
- 추가 패키지 설치 없음
- 이 디렉터리에서 PowerShell 또는 명령 프롬프트 실행
- 하이브리드 검색/`ask` 사용 시 Ollama가 실행 중이고 `embeddinggemma:latest`가 로컬에 있어야 함
- 답변 생성에는 로컬 `gemma4:e4b`가 필요함

## 사용법

현재 디렉터리의 모든 ZIP을 새 DB로 인덱싱합니다.

```powershell
python .\bugreport_index.py index . --db .\bugreports.sqlite3
```

특정 ZIP 여러 개도 지정할 수 있습니다. 출력 DB는 임시 파일에 완성한 뒤 원자적으로 교체됩니다. 기존 DB를 보존하려면 다른 `--db` 이름을 사용하세요.

```powershell
python .\bugreport_index.py index .\a.zip .\b.zip --db .\my-index.sqlite3
```

키워드를 모두 포함하는 청크를 검색하고 각 결과 앞뒤 한 청크를 함께 봅니다.

```powershell
python .\bugreport_index.py search --db .\bugreports.sqlite3 "FATAL EXCEPTION" --limit 5 --context 1
python .\bugreport_index.py search --db .\bugreports.sqlite3 "ANR" --section "SYSTEM LOG" --severity E
```

`--preview-lines 0`은 청크 전문, 기본값 30은 앞 30줄만 출력합니다. `--context 0`은 검색 적중 청크만 출력합니다. 인덱스 요약은 다음과 같습니다.

```powershell
python .\bugreport_index.py stats --db .\bugreports.sqlite3
```

## 임베딩 하이브리드 인덱스 만들기

먼저 위의 `index` 명령으로 FTS DB를 만듭니다. 다음 명령은 기존 DB를 읽기 전용으로 복사하고 각 청크를 제한된 배치로 Ollama `embeddinggemma:latest`에 전달해 **새 DB**에 벡터를 추가합니다. 기존 `bugreports.sqlite3`와 ZIP은 변경하지 않습니다.

```powershell
python .\bugreport_index.py embed-index --db .\bugreports.sqlite3 --output .\bugreports-hybrid.sqlite3
```

기본 배치는 8개이며 `--batch-size 1..64`로 변경할 수 있습니다. 각 입력은 기본 6,000자로 제한되어 대형 보고서나 청크 전체를 한꺼번에 Ollama로 보내지 않습니다. 벡터는 정규화된 little-endian float32 BLOB으로 `embeddings` 테이블에 저장하며 `model`, `dimensions`, 원래 `norm`, `content_chars` 메타데이터도 보존합니다. 같은 출력 경로로 다시 실행하면 완성된 새 DB로 원자 교체되며, `--db`와 `--output`에 같은 경로를 지정하면 안전하게 거부합니다.

하이브리드 검색은 FTS/BM25 상위 후보와 전체 임베딩 코사인 유사도 상위 후보를 RRF(Reciprocal Rank Fusion)로 결합합니다.

```powershell
python .\bugreport_index.py hybrid-search --db .\bugreports-hybrid.sqlite3 "런처가 시작하자마자 죽는 권한 문제" --limit 5
```

기존 `search` 명령은 계속 순수 FTS 검색으로 동작하므로 Ollama 없이도 사용할 수 있습니다.

## 로컬 Ollama로 질문하기

`ask`는 하이브리드 DB에 임베딩이 있으면 질문을 `embeddinggemma:latest`로 벡터화하고 FTS+벡터 결합 검색을 수행합니다. 적중 청크와 앞뒤 문맥을 읽어 출처·줄 범위를 포함한 프롬프트를 답변 모델에 보냅니다. 생성 기본 모델은 **`gemma4:e4b`**, 임베딩 기본 모델은 **`embeddinggemma:latest`**입니다. 임베딩 없는 기존 DB를 지정하면 이전처럼 FTS 검색으로 안전하게 폴백합니다.

준비 상태를 확인합니다.

```powershell
ollama --version
ollama list
```

목록에 `embeddinggemma:latest`와 `gemma4:e4b`가 있고 Ollama 서비스가 실행 중일 때 하이브리드 DB로 질문합니다.

```powershell
python .\bugreport_index.py ask --db .\bugreports-hybrid.sqlite3 "FATAL EXCEPTION의 원인과 영향 프로세스를 설명해 줘"
```

이미 설치된 다른 모델을 사용하려면 `--model`을 명시합니다. 다음 예시의 모델도 먼저 `ollama list`에 존재해야 합니다.

```powershell
python .\bugreport_index.py ask --db .\bugreports-hybrid.sqlite3 "ANR 흔적을 요약해 줘" --model qwen3:8b
```

생성 모델은 `--model`, 임베딩 모델은 `embed-index`, `hybrid-search`, `ask`의 `--embedding-model`로 변경합니다. 임베딩 질의는 DB 생성에 사용한 것과 같은 모델이어야 하며 다르면 중단됩니다. 모델을 바꾸려면 새 출력 DB로 `embed-index`를 다시 실행하세요.

원격 호스트나 다른 포트를 명시하려면 `--ollama-url http://호스트:포트`를 사용합니다. 검색량은 `--limit`, 앞뒤 문맥은 `--context`, 프롬프트 최대 문자는 `--max-context-chars`로 제한할 수 있습니다. 답변 뒤에는 `[S1] ZIP::내부파일:시작줄-끝줄` 형식의 실제 검색 근거 목록이 항상 출력됩니다.

Ollama가 없거나 서비스가 실행 중이 아니면 연결 안내와 함께 종료 코드 2로 안전하게 실패합니다. 필요한 `embeddinggemma:latest`, 기본 `gemma4:e4b`, 또는 명시한 모델이 설치 목록에 없을 때도 API 작업 전에 종료하며 **다운로드나 대체 모델 선택을 하지 않습니다**. 검색 결과가 없으면 생성 모델을 호출하지 않습니다.

## 청크와 메타데이터

도구는 최상위 `------ SECTION ------` 헤더와 logcat 버퍼 경계를 우선 보존하고, 일반 텍스트는 기본 200줄 단위로 제한합니다. 로그 레코드와 Java/native 스택 패턴은 별도 청크로 분류하며 스택은 가능한 한 이어 붙이되 무제한으로 커지지 않습니다.

SQLite의 `reports`에는 ZIP 경로·이름·크기·수정 시각, 주 보고서 엔트리·비압축 크기·인덱싱 시각이 들어갑니다. `chunks`에는 다음 정보가 저장됩니다.

- `section`, `source_entry`, `start_line`, `end_line`, 보고서 내 `seq`
- `kind` (`section`, `log`, `stack`, `text`), `content`
- 감지 가능한 첫/마지막 시간, 태그/프로세스, PID, 최고 심각도, 스택 여부

Python의 SQLite가 FTS5를 지원하면 `chunks_fts`를 사용해 BM25 순위 검색을 수행합니다. 드물게 FTS5가 없으면 자동으로 `LIKE` 기반 검색으로 동작합니다.

## 검색 결과 형식

각 결과 헤더는 다음 형태입니다.

```text
[1] MATCH ZIP이름::ZIP내부주보고서.txt:시작줄-끝줄 (chunk=ID, seq=순서)
    section=... | kind=... | timestamp_start=... | process=... | pid=... | severity=... | stack=yes
```

`MATCH`는 직접 검색된 청크, `CONTEXT`는 `--context`로 확장한 인접 청크입니다. 따라서 ZIP 파일명, 내부 출처, 정확한 원본 줄 범위를 이용해 원문을 추적할 수 있습니다.

## 검증

단위 테스트:

```powershell
python -m unittest -v .\test_bugreport_index.py
```

실제 ZIP 3개, `embeddinggemma:latest`, `gemma4:e4b`에 대한 실행 기록은 `VALIDATION.md`에 요약되어 있습니다. 원문 출력은 `validation-output.txt`, `ollama-validation-output.txt`, `embedding-validation-output.txt`에 있습니다. 생성된 `bugreports.sqlite3`와 `bugreports-hybrid.sqlite3`는 원본 ZIP과 별도 파일이며 삭제해도 ZIP에는 영향이 없습니다.

## 제한 사항

메타데이터는 Android 버전·제조사별 텍스트 형식 차이를 정규식으로 최선 추출합니다. 한 청크에 여러 로그 레코드가 있으면 시간은 범위, 프로세스/PID는 쉼표 목록, 심각도는 가장 높은 수준으로 요약됩니다. 암호화 ZIP, 손상 ZIP, `main_entry.txt`가 없거나 잘못된 ZIP은 오류로 보고하고 원본은 그대로 둡니다.
