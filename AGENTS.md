# CallOps 작업 지침

## 시작점

- [README](README.md) → [하네스와 단계별 완료 기준](docs/harness.md)을 먼저 읽는다.
- 기능·상태·보관 계약의 기준은 [프로젝트 계획](docs/01-project.md)이다. 해당 F/P 절만 추가로 읽는다.
- GPU 설치는 [데스크톱 계획](docs/02-desktop.md), 공용 n8n은 [서버 계획](docs/03-server.md)을 따른다.
- `docs/specs/01-recording-ingestion-classification.md`는 과거 초안이다. 최신 계획과 충돌하면 최신 계획을 따른다.
- 사용자는 P1~P3 구현 착수를 지시했다. 같은 범위의 구현 승인을 반복 요청하지 않는다. 현재 하네스 구축은 제품 기능 완료를 뜻하지 않는다.

## 개발 루프

1. `git status --short`와 관련 코드·명세를 확인하고 기존 변경을 보존한다.
2. 이번 변경의 입력·상태 전이·실패 조건을 정한다. 명세를 바꾸면 코드와 함께 갱신한다.
3. 실제 구현을 호출하는 가장 작은 실패 재현 테스트를 작성하고 수정한다. 명세를 흉내 낸 별도 상태 머신을 테스트 대상으로 만들지 않는다.
4. `python scripts/check.py`와 해당 단계의 `python scripts/check.py --phase P1`(또는 P2/P3)을 실행한다.
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
- 초기 서버는 하나의 앱, 실제 워커는 하나다. Redis·새 브로커·에이전트 프레임워크는 현재 범위에 없다.
- 시계는 주입/patch해 만료를 검증하고 실제 sleep을 쓰지 않는다. 외부 효과는 `unittest.mock`으로 관찰한다.
- P1~P3에서는 가짜 데이터로 개발한다. 운영 배포·실기기·OAuth·드라이버 변경은 그 단계의 사용자 지시 범위를 확인한다.

근거: [공식 AGENTS.md 안내](https://learn.chatgpt.com/docs/agent-configuration/agents-md). 이 파일은 탐색과 실행 규칙이며 전체 제품 명세를 복제하지 않는다.

<!-- AGENTFLOW MANAGED START -->
# AgentFlow project guidance

- Project: callops
- Architecture: Inspect module boundaries; preserve existing architecture
- Stack: Python, flask
- Important directories/module boundaries: .github, deploy, docs, migrations, scripts, src, tests
- Libraries/persistence/state/styling: postgres, sqlite
- Commands: {"lint": null, "typecheck": null, "build": null, "test": null}
- Roles: planner, supervisor
- Follow existing naming, API, security and persistence conventions; inspect context.md.
- Planning and final approval are mandatory. Prototype requires approval by default.
- Do not deploy, mutate production data, send real mail, or charge payments.
- Exchange approved artifacts, never private reasoning or other agents' conversations.
<!-- AGENTFLOW MANAGED END -->
