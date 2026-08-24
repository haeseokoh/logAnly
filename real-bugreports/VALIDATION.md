# 실제 bugreport 검증 결과

검증 일시: 2026-08-25 (Asia/Seoul)

## 실행 환경

- Python 3.12
- 추가 설치 패키지 없음
- SQLite FTS5 활성화

## 실행 결과

`python -m unittest -v test_bugreport_index.py` 결과 2개 테스트가 모두 통과했습니다.

`python bugreport_index.py index . --db bugreports.sqlite3`로 실제 ZIP 3개를 모두 인덱싱했습니다.

| ZIP | 주 보고서 비압축 크기 | 청크 수 |
|---|---:|---:|
| `bugreport-bramble-TQ1A.221205.011-2023-02-03-20-12-41.zip` | 26,376,972 bytes | 2,664 |
| `bugreport-bramble-UQ1A.240205.004-2024-03-26-22-44-59.zip` | 72,897,790 bytes | 3,906 |
| `bugreport-kltetmo-RQ3A.211001.001-2022-12-23-04-52-20.zip` | 21,652,777 bytes | 2,138 |
| **합계** | **120,927,539 bytes** | **8,708** |

인덱싱 완료 시간은 약 31초였습니다. `FATAL EXCEPTION` 검색은 실제 crash 버퍼의 스택 청크를 찾았고, 출처와 줄 범위(예: UQ1A 보고서 14,650–15,049), 시간·프로세스·PID·심각도·스택 메타데이터를 출력했습니다. 앞뒤 인접 청크도 `CONTEXT`로 확장되었습니다.

`ANR` 검색은 세 보고서에서 각각 ANR 섹션을 찾아 3개 적중 청크와 인접 청크를 출력했습니다. 두 검색 명령 모두 종료 코드 0이었습니다.

## 원본 보존 확인

인덱싱·검색 전후 세 ZIP의 SHA-256을 비교해 모두 동일함을 확인했습니다.

- TQ1A: `1CACB810DBDE1AD2F9C08DAD3957118D365AD0BDDF7C2F6895DF142E5CD7AC53`
- UQ1A: `852ECCFA6C0DD383749BD3CF465F1155AF5A34C03C6FB2073F281B8BDE5FB0D0`
- RQ3A: `117072E712E5129DAD99903202B2FBB440203072B565ADD8FA6CC7EA1368C105`

전체 명령 출력과 검색 표본은 `validation-output.txt`에 보존했습니다. 생성물 `bugreports.sqlite3`, `validation-output.txt`, 이 문서는 원본 ZIP과 별도 파일입니다.

## 로컬 Ollama 연동 검증

2026-08-25에 Ollama 0.32.15가 실행 중이며 다음 9개 로컬 모델이 설치된 것을 `/api/tags`로 확인했습니다. 모델 다운로드는 실행하지 않았습니다.

`gemma4:e4b`, `gemma3:12b`, `ministral-3:14b`, `ministral-3:8b`, `qwen3-vl:8b`, `mistral:7b`, `qwen2.5vl:latest`, `qwen3:8b`, `gemma3:4b`

명시된 기본 모델 `gemma4:e4b`로 다음 실제 명령을 실행했습니다.

```powershell
python .\bugreport_index.py ask --db .\bugreports.sqlite3 "FATAL EXCEPTION 원인과 영향 프로세스를 근거와 함께 요약해 줘" --limit 2 --context 1 --max-context-chars 12000
```

종료 코드 0으로 한국어 답변을 받았습니다. 답변은 `com.google.android.apps.nexuslauncher`의 `READ_DEVICE_CONFIG` 권한 거부를 근본 원인으로 분석하고 `[S2]`, `[S4]`를 인용했습니다. 도구가 뒤에 출력한 근거 목록에는 ZIP 내부 주 보고서와 정확한 줄 범위(14,650–15,049 및 15,054–15,462)가 포함되었습니다.

안전 실패도 확인했습니다.

- 설치되지 않은 `definitely-not-installed:model` 지정: API 생성 호출 전 한국어 안내, 종료 코드 2
- 응답하지 않는 `http://127.0.0.1:9` 지정: Ollama 실행 확인 안내, 종료 코드 2
- 기본 모델과 사용자 지정 모델을 설치 목록과 대조하는 단위 테스트 포함, 전체 4개 테스트 통과

Ollama 질의 전후 `bugreports.sqlite3`와 원본 ZIP 3개의 SHA-256이 각각 동일했고 DB `integrity_check`는 `ok`였습니다. 전체 Ollama 답변, 출처, 실패 출력 및 해시는 `ollama-validation-output.txt`에 보존했습니다.

## embeddinggemma 하이브리드 검색 검증

2026-08-25에 이미 설치된 `embeddinggemma:latest`(621MB)를 확인하고 `/api/embed`에 한국어 시험 입력을 직접 보내 768차원 응답을 받았습니다. 새 모델 다운로드 명령은 실행하지 않았습니다.

기존 FTS DB를 읽기 전용으로 두고 다음 명령으로 별도 하이브리드 DB를 생성했습니다.

```powershell
python .\bugreport_index.py embed-index --db .\bugreports.sqlite3 --output .\bugreports-hybrid.sqlite3 --embedding-model embeddinggemma:latest --batch-size 16 --max-embed-chars 4000
```

- 실제 8,708개 청크를 16개 제한 배치로 처리
- 소요 시간 738.36초
- `embeddings` 8,708행, 누락 0
- 모든 벡터 768차원, BLOB 길이 3,072 bytes(768×float32)
- 저장 모델 `embeddinggemma:latest`, 정규화 벡터와 원래 norm/입력 문자 수 저장
- `bugreports-hybrid.sqlite3` 크기 199,565,312 bytes, `integrity_check=ok`

`hybrid-search`로 `런처가 시작하자마자 죽는 권한 문제`를 질의해 FTS+코사인 유사도 RRF 결합 결과와 ZIP 내부 출처·줄 범위를 출력했으며 종료 코드 0이었습니다. 이어 하이브리드 DB에 `FATAL EXCEPTION에서 READ_DEVICE_CONFIG 권한 오류로 종료된 프로세스와 원인은?`을 `ask`로 질의했습니다. `embeddinggemma:latest`가 질의를 임베딩하고 결합 검색한 근거를 `gemma4:e4b`가 분석해 `com.google.android.apps.nexuslauncher`, `SecurityException`, `READ_DEVICE_CONFIG` 권한 거부를 출처 `[S2]`, `[S5]`와 함께 답했으며 종료 코드 0이었습니다.

기존 순수 FTS `search`도 다시 실행해 정상 결과를 확인했습니다. 최종 단위 테스트는 5개 모두 통과했습니다. 기존 `bugreports.sqlite3` SHA-256은 종전과 같은 `15EAF4C0...031F3`, 원본 ZIP 3개 해시도 종전과 동일합니다. 전체 진행률, 하이브리드 검색 및 ask 출력은 `embedding-validation-output.txt`에 보존했습니다.
