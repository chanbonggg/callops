# P1 설정·DB 기반

P1은 Python 3.12와 psycopg 3.3.3을 사용한다. SQL은 직접 작성하고 트랜잭션으로 적용한다. 첫 마이그레이션은 recordings와 마이그레이션 이력을 만든다. 이후 추가한 HTTP 서버와 큐는 [P2/P3 안내](p2-p3-server.md)를 따른다. ORM·GPU 연동은 추가하지 않았다.

## 설치와 검사

제품/DB 테스트에는 [requirements.txt](../requirements.txt)의 고정 의존성이 필요하다. 하네스 기본 검사는 여전히 표준 라이브러리만 사용한다.

```text
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/test_postgres.py --pg-bin E:/PostgreSQL/bin
```

Windows 예시이며 PostgreSQL 실행 파일 경로는 실제 설치 위치로 바꾼다. Linux에서는 `.venv/bin/python`과 설치된 PostgreSQL bin 경로를 사용한다. 실행 파일 디렉터리의 initdb/pg_ctl만 사용하며 기존 서비스 데이터 디렉터리는 읽거나 변경하지 않는다.

네이티브 테스트 실행기는 저장소의 무시된 `tmp/callops-p1-*`에 새 클러스터를 만들고 임의의 로컬 포트·임시 비밀번호를 사용한다. 실제 data_directory를 대조한 뒤 callops_test DB만 생성한다. 테스트 종료 시 자신의 클러스터를 중지하고 해당 임시 디렉터리만 정리한다. 종료 상태를 확인할 수 없으면 데이터를 지우지 않는다. Windows 제한 토큰에서 pg_ctl 시작이 실패할 수 있으며, 이때 DB 테스트를 통과로 간주하지 않는다.

기존 서비스 방식의 테스트는 빈 전용 PostgreSQL에 `CALLOPS_TEST_ADMIN_URL`을 명시하고 `python scripts/test_postgres.py --service --phase P3`를 실행한다. 이미 CallOps 스키마/역할/테이블이 있으면 초기화하거나 지우지 않고 거부한다. 운영 URL이나 일반 DATABASE_URL은 사용하지 않는다.

`python scripts/check.py --phase P1` 직접 실행에는 별도로 준비된 새 테스트 DB의 `CALLOPS_TEST_DATABASE_URL`, `CALLOPS_TEST_MIGRATION_DATABASE_URL`이 필요하다. 위 실행기가 생성·주입하므로 일반 개발에서는 실행기를 사용한다. 테스트는 반복 실행 때 데이터를 삭제하는 방식이 아니라 매번 빈 DB로 시작한다.

## 제품 설정과 명령

[설정 예시](../.env.example)는 설명용이며 자동 로드하지 않는다. 환경변수를 명시적으로 주입한다.

| 변수 | 사용 프로세스 | 제약 |
|---|---|---|
| CALLOPS_DATABASE_URL | 앱 readiness | callops_app, PostgreSQL URI, 호스트·포트·DB·비밀번호·sslmode 필수 |
| CALLOPS_MIGRATION_DATABASE_URL | 마이그레이션만 | callops_migrator, 앱에 제공하지 않음 |
| CALLOPS_DATA_ROOT | 공통 | 저장소 밖의 절대 디렉터리 경로 |

로컬 루프백 외 호스트는 sslmode=verify-full을 요구한다. 인증서 구성은 실제 배포 단계에서 확정한다. 접속 URL·비밀번호·SQL 값은 CLI 오류 출력과 Settings repr에 노출하지 않는다. 제품 환경변수는 DB 테스트로 자동 상속하지 않는다.

```text
python scripts/manage.py migrate
python scripts/manage.py check
```

첫 명령은 명시적인 스키마 변경, 두 번째는 설정·앱 계정 권한·마이그레이션 버전의 읽기 검증이다. 앱 시작 시 자동 마이그레이션하지 않는다. check가 성공해도 HTTP 서버나 파일 수집이 실행되는 것은 아니다.

## 권한과 마이그레이션

- [bootstrap.sql](../deploy/server/bootstrap.sql)은 관리자가 **새 전용 DB**에서 한 트랜잭션으로 실행할 역할/스키마 초기 설정이다. 비밀번호는 별도 보안 경로로 지정한다. 이 작업의 운영 실행은 아직 하지 않았다.
- 마이그레이터는 callops 스키마를 소유하지만 superuser/createdb/createrole 권한이 없다. 앱은 스키마 사용과 업무 테이블 SELECT/INSERT/UPDATE/DELETE만 허용한다.
- 앱은 DDL·마이그레이션 이력 변경·마이그레이터 SET ROLE을 할 수 없다. PostgreSQL 실제 권한으로 검증한다.
- [001_initial.sql](../migrations/001_initial.sql)은 파일 해시와 함께 기록한다. 적용한 파일 변경·알 수 없는 DB 버전은 거부하며 재실행은 멱등이다.
- 트랜잭션 자문 잠금으로 동시 마이그레이션을 직렬화하고 실패한 SQL의 DDL/이력은 함께 롤백한다. 기존 마이그레이션 수정 대신 다음 번호 파일을 추가한다.
- 파괴적 down migration이나 데이터 초기화 명령은 제공하지 않는다. `002_ingestion_queue.sql`은 P2 수집·P3 큐, `003_recording_metadata.sql`은 상대 이름 열, `004_schedule_candidates.sql`은 일정 후보와 승인 기록, `005_mock_calendar.sql`은 모의 전송 상태와 이벤트 식별 기록, `006_manual_schedules.sql`은 직접 입력 일정을 추가한다.
- `007_schedule_model.sql`은 직접 입력 일정을 `schedules`로 옮기고 통화 일정의 마지막 승인 내용을 이관한다. 기존 일정과 모의 이벤트 ID를 보존한다. `008_processing_integrity.sql`은 실행 시도에 통화 ID를 채우고 작업·입력·결과가 같은 통화에 속하도록 외래 키를 적용한다.

## 검증 대상

설정 누락/잘못된 계정/원격 비암호화 연결/저장소 내부 경로 거부, 비밀값 repr 제외, 미적용 DB readiness 실패, 빈 DB 적용·재실행, 앱 DML 및 DDL 차단, 잘못된 계정의 migration 거부, SQL 오류 롤백, 적용 파일 변조 거부를 시험한다. 실제 수집·세션 인증·동시 claim은 P2/P3에서 검증한다.

로컬 결과: Python 3.12.4 / psycopg 3.3.3 / PostgreSQL 18.0에서 `python scripts/test_postgres.py --pg-bin E:/PostgreSQL/bin` 실행, 하네스 6개·P1 3개 테스트 통과. 테스트 후 임시 서버 종료·정리 완료. 현재 저장소에는 GitHub Actions 워크플로가 없으며 원격 CI 검증은 하지 않았다.
