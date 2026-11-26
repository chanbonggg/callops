# CallOps 개발 하네스

하네스는 에이전트가 같은 입력·명령·완료 기준으로 작업하게 하는 저장소 구성이다. 운영 서비스를 실행하거나 모델의 정답을 보장하는 장치는 아니다.

## 실행

Python 3.12와 Git이 필요하다. 기본 하네스는 추가 패키지 없이 실행하며, P1은 [고정 의존성과 실제 PostgreSQL](p1-foundation.md)이 필요하다. 저장소 루트에서 실행한다.

```text
python scripts/check.py
python scripts/check.py --phase P1
python scripts/check.py --phase P2
python scripts/check.py --phase P3
```

기본 명령은 Git에 포함될 파일의 금지 데이터 경로, Python 구문, Markdown 상대 파일 링크·코드 블록, 하네스 자체 테스트를 검사한다. 파일 내용에서 모든 비밀정보를 탐지하는 스캐너나 네트워크 샌드박스는 아니다. 외부 링크와 Markdown anchor는 자동 검사하지 않는다.

`--phase P2`는 기본 검사와 P1·P2를 함께 실행한다. 테스트가 없거나 skip/expectedFailure가 있으면 실패한다. P1~P3는 pgvector가 설치된 실제 PostgreSQL의 빈 테스트 DB에서 실행한다. [DB 환경](p1-foundation.md) · [P2/P3 실행](p2-p3-server.md)

2026-11-25 Ubuntu PostgreSQL 16.15 임시 클러스터에서 하네스 7·P1 3·P2 19·P3 74, 총 103개가 통과했다. 2026-11-24 Windows PostgreSQL 18.0에서는 로그인 화면 반영 뒤 총 99개가 통과했다. Ubuntu의 임시 클러스터는 TCP 루프백으로만 실행한다. 홈서버 배포 환경 검증은 별도다. 미리 준비한 빈 전용 PostgreSQL을 사용할 때는 `python scripts/test_postgres.py --service --phase P3`를 쓸 수 있다. GitHub Actions 워크플로는 저장소에 구성되어 있지 않다.

데스크톱 AI 모의 검사는 D1 Python 환경에서 `python -m unittest discover -s ai-worker/tests -v`로 실행한다. 이 환경에 Pydantic 2, Ollama Python 클라이언트와 전사 검사 의존성이 들어 있다. [D1 패키지 스냅샷](../ai-worker/requirements-d1.txt)

## 파일의 역할

| 경로 | 역할 |
|---|---|
| `AGENTS.md` | 에이전트의 시작점·불변 조건·작업 순서 |
| `docs/01-project.md` | 제품 요구사항과 승인 범위의 기준 |
| `scripts/check.py` | 로컬/CI 공통 검사, 누락된 단계 검사 실패 처리 |
| `tests/harness/` | 검사 도구 자체의 회귀 테스트 |
| `tests/fixtures/scenarios.json` | T01~T10 및 중복 사례의 합성 대본·기대 결과 |
| `tests/fixtures/d4-analysis-edges.json` | D4의 합성 담당자·조건부 할 일·기준 시각·자정 경계 대본 |
| `tests/fixtures/rag-development.json` | 개발용 가상 통화 10건·질문 20개와 실제 전사 구간 ID를 참조하는 정답 근거 |
| `tests/fixtures/rag-final.json` | 최종 평가용 가상 통화 10건·질문 20개. 하네스가 개발 세트와의 중복, 근거 구간과 질문 범위를 검사한다. |
| `tests/p1/` | 설정·실제 DB·마이그레이션·권한 경계 검사 |
| `scripts/test_postgres.py` | 기존 서비스와 분리된 새 PostgreSQL에서 지정 단계 실행·정리 |
| `tests/p2/`, `tests/p3/` | 수집·인증·분류·삭제 및 실제 큐/API 회귀 검사 |

테스트 디렉터리는 `__init__.py`를 포함하고 `test_*.py`를 사용한다. 하네스는 테스트 개수와 실패를 검사할 뿐 테스트의 내용 충실도를 판정하지 못한다. 다음 표를 코드 리뷰에서 확인한다.

## P1~P3 완료 증거

| 단계 | 실제 구현을 통해 확인할 것 | 필요한 환경 |
|---|---|---|
| P1 | 설정 누락 시 시작 실패, 별도 빈 DB 마이그레이션, 앱/마이그레이션 역할 분리 | 전용 테스트 PostgreSQL, 운영 credential 금지 |
| P2 | 미인증/CSRF 차단, 부분 파일·형식 위장·경로 탈출 거부, T04 AI 호출 0회, 중복 수신, 만료/사적 선택 후 사본 청소 | 임시 디렉터리, 테스트 DB, 제어 가능한 시계, 호출 관찰용 mock |
| P3 | 두 DB 연결의 동시 claim, 구 임대 거부, 준비 중 heartbeat, 결과 준비 보고·ACK 유실의 멱등성, 실행/업로드 기한 분리, 원음·산출물 만료와 취소 후 청소·복구 | 실제 PostgreSQL 큐/API, 결정적인 가짜 워커 |

`--phase P3`는 큐뿐 아니라 이후 추가한 서버 기능의 통합 테스트도 `tests/p3/`에서 실행한다. 파일별 범위는 아래와 같다. 제품 계약의 P4~P8 단계 이름과는 별개의 실행 묶음이다.

| `tests/p3/` 파일 | 확인 범위 |
|---|---|
| `test_queue.py` | 큐·임대·기한·ACK·취소 청소 |
| `test_real_worker.py` | 실제 결과 형식의 선택 수락, 고정 전사 분석, 결과 상태와 무음 |
| `test_audio_formats.py` | 형식별 수신·호환 재생 |
| `test_transcript_views.py` | 합성 전사·요약, 인용 재생, 전사 검색, 근거 검색, 통화 질문 |
| `test_schedule_candidates.py` | 일정 후보 검토, 직접 입력 일정, 월간 캘린더, 모의 전송 |
| `test_calendar_sync.py` | 연결 일정 양방향 동기화·충돌 해결 |
| `test_tasks.py`, `test_task_candidates.py` | 직접 입력 할 일, 통화 할 일 후보 |
| `test_retention.py` | 30일·90일 만료와 부분 청소 |

가짜 워커는 P3의 **실제 API**를 호출하고 합성 결과만 반환한다. Starlette ASGI 테스트 클라이언트와 실제 PostgreSQL로 인증·상태·파일 경로를 실행하며 대체 상태 머신을 만들지 않는다. Calendar는 PostgreSQL에 저장하는 모의 제공자(`schedule/calendar.py`)로만 검증한다. 실제 Google/n8n 어댑터는 아직 없으며 도입할 때 `Mock`으로 요청 횟수와 승인 스냅샷을 검사한다. 만료 검증은 주입한 시계를 사용한다.

시험용 DB는 별도 이름·계정으로 구성하고 테스트 설정을 명시적으로 지정한다. 일반 `DATABASE_URL`이나 운영 `.env`를 자동 로드하지 않는다. DB가 없으면 단계 검사는 실패해야 한다. 데이터 초기화 코드는 테스트 대상 확인 없이 DROP/TRUNCATE하지 않는다.

## 대본과 오디오

합성 대본은 [fixture](../tests/fixtures/scenarios.json)에 있다. 기대 결과를 모델 출력에 맞춰 느슨하게 고치지 않는다. 근거·불확실성·후보 수를 검증하며 문장 전체의 문자 일치는 요구하지 않는다.

T04는 AI에 입력하지 않는 수집/분류 시험이고, T01-copy는 T01 파일의 바이트 복사로 생성한다. T06/T08/T09는 실제 모델 평가 때 정정·충돌·취소가 다른 조각에 오도록 구성한다. fixture의 짧은 문장만으로 컨텍스트 초과 시험을 통과했다고 보고하지 않는다. T05의 잡음/무음은 별도 합성 오디오가 필요하다. 비프음·무음 WAV는 파일 처리 시험에는 쓸 수 있지만 한국어 STT 품질 증거가 아니다.

D4 추출은 위 대본의 화자 표시와 원본 ID를 사용한다. 워커 모의 시험과 실제 모델 평가는 별도 명령으로 확인한다. 검색 품질은 개발 보고서와 같은 설정으로 최종 세트를 실행하고, 첫 고정 평가와 동일 문항의 재평가를 각각 기록한다. [AI 모듈 실행 안내](../ai-worker/README.md#d4-정답-전사-평가--wsl-데스크톱) · [D4 검색 평가](../ai-worker/README.md#d4-검색-평가)

D5 모의 시험은 워커를 재시작해 결과 키와 원래 바이트가 유지되는지, 입력 버전·해시 거부와 연산 종료 차단을 확인한다. 실제 GPU 시험은 T01·T06·T09, 할 일·미확인 시각·명령이 포함된 합성 발화와 T04·무음을 별도 루프백 HTTPS에서 실행한다. [D5 실행](../ai-worker/README.md#d5-워커-실행--wsl-데스크톱) · [검증 기록](02-desktop.md#2026-11-25-d5-로컬-워커-검증)

질문 모델 모의 시험은 D5 엔진의 임베딩 배치, 잘못된 차원과 영벡터, 근거 밖 인용, 보류, 응답 한도 및 취소 이후 결과 차단을 검사한다. `app.rag_smoke`는 별도로 실행해 실제 GPU에서 두 모델의 적재와 합성 답변을 검증한다. 웹·pgvector와의 통합은 권한·입력 유효성·인덱스 청소를 포함한 서버 시험에서 확인한다. [질문 모델 테스트](../ai-worker/tests/test_rag_worker.py) · [합성 GPU 시험](../ai-worker/README.md#wsl-gpu-합성-시험)

실제 오디오는 Git 밖의 임시 경로에서 생성/보관한다. 전사 평가와 STT 평가는 구분하고 P4/P5부터 프로젝트 9절의 고정 세트와 시간 기준을 적용한다.

## 현재 상태와 다음 작업

[전체 진행 체크리스트](progress-checklist.md)에서 관리한다. 이 문서는 검사 방법과 완료 기준을 관리한다. 단계 완료 시 체크리스트와 해당 기능의 검증 기록을 갱신하고, 승인 범위가 바뀔 때만 프로젝트 계약을 수정한다.
