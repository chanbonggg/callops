# P2/P3 서버 구현과 재현

이 구현은 가짜 데이터로 수집·분류·삭제와 PostgreSQL 큐 계약을 검증한다. 실제 통화, Whisper/Ollama, Google, n8n, 동기화 서비스에 연결하지 않는다. 실제 모델 결과는 아직 허용하지 않는다.

## 실행 환경

Python 3.12, `requirements.txt`의 psycopg 3.3.3과 Flask 3.1.2를 사용한다. Flask는 로그인·서버 렌더링·WSGI 테스트 클라이언트를 한 앱에서 제공하기 위해 선택했다. DB 준비와 역할 분리는 [P1 안내](p1-foundation.md)를 따른다. `002_ingestion_queue.sql`을 명시적으로 적용해야 앱이 시작된다.

```text
python -m pip install -r requirements.txt
python scripts/check.py
python scripts/test_postgres.py --pg-bin E:/PostgreSQL/bin --phase P2
python scripts/test_postgres.py --pg-bin E:/PostgreSQL/bin --phase P3
```

마지막 두 명령은 각각 새 테스트 클러스터를 생성하고 `scripts/check.py --phase P2/P3`를 실행한 뒤 정리한다. 기존 서비스·운영 DB는 사용하지 않는다. CI는 새 PostgreSQL 18.6 서비스에서 `--service --phase P3`를 실행하도록 구성했다. Windows 로컬 검증 대상은 PostgreSQL 18.0이며 원격 CI는 아직 실행하지 않았다.

## 수집·분류

- 데이터 루트는 Git 밖의 전용 경로다. `sync-inbox`, `quarantine`, `artifacts`만 관리한다. 휴대전화 원본 경로를 받지 않는다.
- 초기 입력은 PCM WAV, 최대 500MiB·2시간·1~2채널이다. 확장자 대신 WAV 헤더와 실제 프레임 길이를 검사한다. m4a/amr 변환은 아직 없다. 상한 부하·실기기 호환 검증은 별도다.
- 수신 도구가 파일을 닫고 완료를 확정한 뒤 제공한 SHA-256을 입력한다. 앱이 파일 크기 안정만 보고 완료 신호를 만들어내지 않는다. 복사 전후 속성·두 번의 해시·격리 사본 판독을 검사한다.
- SHA-256으로 녹음을 중복 제거하고 수신별 사본을 기록한다. 파일명은 화면·로그에 노출하지 않으며 경로 구분자·스트림·심볼릭 링크·Windows reparse point를 거부한다. 전용 루트와 그 상위 디렉터리는 신뢰된 서비스 계정만 수정할 수 있어야 한다. 실제 ACL 구성은 C1/C7 검증 대상이다.
- 미분류·보류는 최초 수신 72시간 후 만료한다. 보류는 기한을 연장하지 않는다. 업무 분류와 STT 작업 INSERT는 같은 트랜잭션이다. 중복 승인으로 작업을 늘리지 않는다.
- 사적 선택/미분류 만료는 inbox·격리·파생 파일을 청소한다. 삭제 실패나 워커 미확인은 완료로 표시하지 않는다. 같은 콘텐츠의 재수신은 새 AI 작업 없이 청소한다. 이름이 재사용되어 다른 콘텐츠가 들어오면 기존 삭제 요청으로 지우지 않는다.
- 거부된 파일은 처음 거부한 때부터 24시간 청소 대상으로 등록한다. 파일 정체성이 바뀌면 청소 실패로 남기며 새 파일을 임의 삭제하지 않는다.

가짜 파일을 전용 inbox에 수신 완료한 뒤 사용하는 명령:

```text
python scripts/manage.py ingest --name completed.wav --sha256 PRODUCER_COMPLETION_SHA256
python scripts/manage.py maintain
```

`maintain`은 만료·사본 청소·임대 회수·파일/DB 복구를 한 번 실행한다. 주기 실행 서비스는 아직 설치하지 않았다. 운영 전 C1에서 주기를 설정해야 한다. 조회 GET은 만료 데이터를 차단하지만 삭제나 업무 상태 변경을 수행하지 않는다.

DB가 불가하면 앱은 신규 격리 복사 전에 실패한다. 실제 동기화 수신을 멈추는 어댑터는 연결하지 않았으므로 C2/C7의 실제 수신 중단 시험까지 완료한 것은 아니다. 사용자가 수동으로 inbox에 쓰는 것까지 막는다고 해석하지 않는다.

## 로그인 화면

환경 변수는 `.env.example`에 이름만 설명하며 자동 로드하지 않는다. 서버에 소유자 이름, Werkzeug `generate_password_hash`로 생성한 scrypt 해시, 서로 다른 충분히 긴 세션 비밀값·워커 토큰을 명시해야 한다. 예제 placeholder는 실제 비밀값이 아니다.

```text
python scripts/manage.py serve --cert ABSOLUTE_CERT_PATH --key ABSOLUTE_PRIVATE_KEY_PATH
```

이 명령은 `https://localhost:8443/login`에서 로컬 개발 서버만 실행한다. 인증서·키 생성과 운영 WSGI/TLS 배포는 자동 수행하지 않는다. 신뢰 호스트는 localhost/127.0.0.1로 제한했다. 운영 도메인과 프록시 신뢰 설정은 C1에서 확정한다.

로그인 CSRF, Secure/HttpOnly/SameSite 쿠키, DB 세션 8시간 만료, 로그아웃 시 서버 세션 폐기, 로그인 실패 제한을 적용했다. 목록·음성·분류 모두 사용자 세션이 필요하다. 분류/로그아웃은 POST와 CSRF를 검사한다. 워커 Bearer 토큰으로 사용자 작업을 수행할 수 없다. 브라우저 보안 설정은 [Flask 공식 안내](https://flask.palletsprojects.com/en/stable/web-security/)를 참고했다.

목록은 최근 200건이며 페이지 이동·원격 배포·브라우저 시각 검수는 아직 없다. 음성 응답은 한 파일을 메모리로 읽으므로 최대 크기 동시 재생의 성능은 검증하지 않았다.

## 큐와 가짜 워커

| 요청 | 목적 |
|---|---|
| `POST /worker/claim` | 승인된 작업의 임대와 고정 입력 식별자 발급 |
| `GET /worker/jobs/{id}/input` | 할당된 워커/현재 임대의 입력만 조회 |
| `POST /worker/jobs/{id}/heartbeat` | 임대만 갱신 |
| `POST /worker/jobs/{id}/start` | 요청 ID에 고정된 실행 기한 확인 |
| `POST /worker/jobs/{id}/prepared` | spool 요청 ID·정확한 해시·크기·생성 시각 고정 |
| `POST /worker/jobs/{id}/result` | 같은 결과 바이트 수락 및 멱등 ACK |
| `POST /worker/jobs/{id}/failed` 또는 `waiting-file` | 제한 재시도 또는 파일 대기 반환 |
| `POST /worker/cleanup` | 자기 장비의 사본별 청소 조회/종료·삭제 확인 |

작업 키는 녹음·단계·파이프라인 버전으로 유일하다. claim에 행 잠금과 `SKIP LOCKED`를 사용한다. 초기 한 서버의 파일/DB 경합은 공통 PostgreSQL advisory lock으로 직렬화했다. 여러 서버의 처리량 최적화는 아직 하지 않는다.

임대는 5분이며 준비 10분, STT 실행 60분, 분석 실행 30분, 업로드 30분이다. heartbeat는 상태별 기한을 연장하지 않는다. 입력 만료와 spool 24시간도 적용한다. 모든 API 시간은 UTC로 반환한다. 상태 변경/응답 유실 재요청은 같은 요청 ID를 사용한다. 구 임대·다른 워커·다른 입력 버전·다른 결과 바이트는 거부한다. 이미 수락한 결과는 나중에도 원문 없이 같은 ACK를 반환한다.

실행 오류는 초기 시도와 추가 3회(1/5/30분) 후 수동 확인으로 멈춘다. 파일 대기·오프라인 임대 만료는 모델 실패 횟수에 넣지 않는다. 종료와 사본 삭제가 미확인인 워커는 다음 작업을 받지 않는다. 청소 조회/확인은 활성 임대 없이 자기 copy key와 요청 버전으로 수행한다.

수락 파일은 part 기록·fsync·rename 후 artifact 참조, 작업 성공, 다음 분석 작업을 한 DB 트랜잭션으로 저장한다. 재시작 때 참조 없는 생성 파일을 정리하고, 참조 파일이 없으면 INPUT_MISSING으로 수동 확인한다. 프로세스/DB 실패 복구를 검증하며 전원 차단에 대한 파일시스템 내구성 시험은 하지 않았다.

`src/callops/fake_worker.py`는 실제 Flask API를 통해 합성 결과를 처리한다. 전사 ACK 후 로컬 파일을 지워도 새 워커가 서버 artifact를 받아 분석한다. 결과에는 job·attempt·input ID/버전/해시가 포함된다. P3 결과는 `synthetic: true`인 고정 스키마만 수락한다. 실제 STT 구간·분석 근거 검증과 장시간 워커 heartbeat 스레드·연산 프로세스 종료·지속 spool 재개는 P4/P5/D5 구현 대상이다.

## 확인 범위

2026-09-20 로컬 결과: Python 3.12.4 / PostgreSQL 18.0에서 `--phase P3` 전체 29개(하네스 6, P1 3, P2 10, P3 10) 통과, skip 없음. 임시 클러스터는 종료·정리했다. 발견한 UTC 응답 표기 불일치와 로그인 제한 조건을 수정한 뒤 재검증했다.

P2 테스트는 인증/CSRF/세션 폐기·중복·사적 재수신·만료·변경 중 파일·위장/부분 파일·거부 파일 청소·승인 트랜잭션 롤백·DB 장애 수신 중단을 검사한다. 링크 속성 거부는 OS 속성을 주입한 회귀 검사이며 실기기 ACL 시험은 아니다.

P3 테스트는 두 실제 DB 연결의 동시 claim, 실제 API 가짜 워커, 늦은 임대, 준비 heartbeat, 고정 시작/업로드 기한, ACK 재전송, 제한 재시도, 입력 버전 불일치, 취소·워커별 청소, rename 뒤 DB 장애·유실 파일 복구를 검사한다. 시계만 주입하고 실제 sleep·SQLite·대체 상태 머신은 쓰지 않는다.

실제 모델 품질, GPU 중단 보장, 운영 전송 중단, 30/90일 전체 보관 정책의 청소, Calendar 등록은 이 시험의 성공 범위가 아니다. 사용자 지시에 따라 Notion 개발일지는 생성하지 않는다.
