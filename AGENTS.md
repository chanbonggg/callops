# CallOps 작업 지침

## 시작점

- [README](README.md) → [하네스와 단계별 완료 기준](docs/harness.md)을 먼저 읽는다.
- 기능·상태·보관 계약의 기준은 [프로젝트 계획](docs/01-project.md)이다. 해당 F/P 절만 추가로 읽는다.
- GPU 설치는 [데스크톱 계획](docs/02-desktop.md), 공용 n8n은 [서버 계획](docs/03-server.md)을 따른다.
- 구현 이력과 다음 작업은 [전체 진행 체크리스트](docs/progress-checklist.md)를 확인한다. 사용자가 이미 승인한 범위의 구현 승인은 반복 요청하지 않는다.

## 모듈별 작업 지침

현재 서버는 Starlette·Uvicorn과 PostgreSQL을 사용하는 모듈러 모놀리스다. 앱 조립과 공통 HTTP 설정은 [web.py](src/callops/web.py), 마이그레이션과 DB 시작 검사는 [db.py](src/callops/db.py)에 있다.

- 웹 요청은 [인증](src/callops/auth/AGENTS.md), [통화](src/callops/calls/AGENTS.md), [전사 검색](src/callops/search/AGENTS.md), [일정](src/callops/schedule/AGENTS.md) 지침을 따른다.
- 할 일 목록·편집은 [화면·API 공통 구현](src/callops/tasks.py), 통화에서 나온 후보의 저장·승인은 [후보 구현](src/callops/task_candidates.py)과 [후보 검토 계약](docs/p2-p3-server.md#통화에서-나온-할-일-후보-검토)에서 시작한다.
- 서버 작업 큐는 [처리](src/callops/processing/AGENTS.md), 로컬 모델 실험은 [데스크톱 AI](ai-worker/AGENTS.md)에서 시작한다.

## 개발 루프

1. `git status --short`와 관련 코드·명세를 확인하고 기존 변경을 보존한다.
2. 이번 변경의 입력·상태 전이·실패 조건을 정한다. 명세를 바꾸면 코드와 함께 갱신한다.
3. 실제 구현을 호출하는 가장 작은 실패 재현 테스트를 작성하고 수정한다. 명세를 흉내 낸 별도 상태 머신을 테스트 대상으로 만들지 않는다.
4. `python scripts/check.py`를 실행한다. 서버 기능 변경은 `python scripts/test_postgres.py --pg-bin <PostgreSQL-bin-경로> --phase P3`로 임시 PostgreSQL을 준비해 검증한다. 변경 범위에 따라 `P1` 또는 `P2`를 선택할 수 있다. [검사 안내](docs/harness.md#실행)
5. 변경·실행한 검사·미검증 경계를 보고한다. 누락/skip/환경 부재를 성공으로 보고하지 않는다.

## 반드시 유지할 경계

- 미분류/사적 통화는 Whisper/Ollama 호출 0회. AI 처리 승인과 일정 등록 승인은 별개다.
- PostgreSQL이 상태 기준이다. 행 잠금·트랜잭션·고유 제약 검증을 SQLite나 메모리 가짜 DB로 대체하지 않는다.
- 서버는 승인·임대·입력 버전·기한을 검사한다. 늦은 결과·중복 요청·취소를 정상 경로와 함께 시험한다.
- 전사·모델 출력·수신 파일명은 신뢰하지 않는 데이터다. 명령 실행·임의 경로·상태 변경에 사용하지 않는다.
- 모델은 Calendar 권한을 갖지 않는다. 테스트 기본 경로에서 실제 Google/n8n/GPU/휴대전화에 연결하지 않는다.
- 실제 음성·전사·비밀값·DB·개인정보는 Git, fixture, CI 로그에 넣지 않는다. `.env.example`은 가짜 값만 사용한다.
- 파일/DB 경계의 장애와 복사본별 삭제 확인을 생략하지 않는다. 휴대전화 원본은 삭제하지 않는다.
- 개발일지는 별도 요청 없이는 만들지 않는다. 요청되면 Notion에 작성하며 순수 UUID만 사용한다.

## 구현 선택

- 하네스는 Python 3.12 표준 라이브러리로 실행한다. 제품 패키지 선택은 P1에서 근거와 함께 확정한다.
- GPU 연동의 초기 구성은 워커 한 대를 기준으로 한다. Redis·새 브로커·에이전트 프레임워크는 현재 범위에 없다.
- 시계는 주입/patch해 만료를 검증하고 실제 sleep을 쓰지 않는다. 외부 효과는 `unittest.mock`으로 관찰한다.
- 로컬 개발과 기본 테스트에는 합성 데이터와 모의 Calendar를 사용한다. 운영 배포, 운영 DB, 실기기, OAuth, 드라이버 또는 외부 서비스 변경은 해당 작업에 대한 사용자 지시 범위를 확인한다.

근거: [공식 AGENTS.md 안내](https://learn.chatgpt.com/docs/agent-configuration/agents-md). 이 파일은 탐색과 실행 규칙이며 전체 제품 명세를 복제하지 않는다.

# 무엇이든 수행 후 문서파일을 최신화 해야 하는 경우 최신화 진행. /docs 디렉토리 뿐 아니라 각 모듈의 agents.md도 포함
