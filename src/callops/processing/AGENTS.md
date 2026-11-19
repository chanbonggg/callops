# 처리 모듈

## 책임과 구현 의도

PostgreSQL의 `jobs`와 `attempts`로 서버 작업 큐를 운영한다. `queue.py`가 상태 전이를 소유하고 `routes.py`가 워커 HTTP 요청을 전달한다. `fake_worker.py`는 운영과 같은 API를 사용해 GPU 없이 계약을 검증한다. [서버 큐 안내](../../../docs/p2-p3-server.md#큐와-가짜-워커)

## 작업 흐름

- 업무 승인 → STT claim → 입력 확인 → start → prepared → result → ACK → ANALYSIS 순서로 진행한다. `lookup()`은 워커·임대와 작업의 연결을, `active()`는 현재 시도·통화 승인·입력 보관·단계별 기한을 확인한다.
- 준비·실행·업로드 기한은 heartbeat와 별개다. 결과 바이트와 요청 키는 prepared 때 고정한다. 동일 업로드의 ACK 재요청은 기존 artifact ID를 반환한다.
- 실행 시도에는 작업의 통화 ID를 함께 저장한다. 복합 외래 키가 작업·입력·결과의 통화 범위와 활성 시도의 작업 소속을 검증한다. [큐 무결성 제약](../../../migrations/008_processing_integrity.sql)
- 결과는 `.part` 기록·fsync·rename 후 artifact, 다음 작업 또는 일정·할 일 후보와 함께 DB에 반영한다. 성공 직전에 기한을 재검사한다. DB 커밋 여부가 불확실한 파일은 통화 모듈의 `recover()`에서 정리한다.
- 원음·입력 전사가 만료되면 통화 서비스가 관련 미완료 작업을 `INPUT_EXPIRED`로 취소하고 사본 청소를 요청한다. `Queue.reap()`은 같은 시각을 기준으로 임대를 회수한다. 이미 수락한 결과의 ACK 재요청은 기존 ID를 반환하며 파일을 다시 기록하지 않는다. [만료 통합 테스트](../../../tests/p3/test_retention.py)
- 서버 수락 형식은 `synthetic: true`다. v1 텍스트는 기존 결과 호환에 쓰고, v2 STT는 시간 구간, v2 ANALYSIS는 고정 전사에 근거한 일정 후보를 사용한다. [결과 스키마](transcript.py) · [후보 모듈](../schedule/AGENTS.md)
- v2 분석 결과는 선택적으로 `summary.text`와 `summary.segment_ids`를 받는다. 근거 ID는 입력에 고정된 전사에서 검증한다. [요약 JSON과 검증 규칙](../../../docs/p2-p3-server.md#합성-요약-결과-스키마와-조회)
- 선택 `tasks` 배열의 제목·기한·근거 구간은 저장 전에 검증한다. 후보 하나가 잘못되면 전체 분석 결과를 거부한다. [합성 할 일 후보 계약](../../../docs/p2-p3-server.md#통화에서-나온-할-일-후보-검토)
- 가짜 STT 워커는 다운받은 원음의 해시를 확인하고 [수신 오디오 검증](../calls/audio.py)의 디코딩 길이로 합성 구간을 만든다.

## 변경 시 확인

- 사적·미분류 통화에 처리 입력을 제공하지 않는다. 취소 후 워커의 정지·삭제 확인이 도착할 때까지 같은 워커에 새 작업을 주지 않는다.
- 실제 PostgreSQL과 주입한 시계로 동시 claim, 기한 경계, ACK 유실, 취소 중 결과, 파일·DB 장애를 검증한다. [큐 테스트](../../../tests/p3/test_queue.py) · [일정 결과 테스트](../../../tests/p3/test_schedule_candidates.py)
