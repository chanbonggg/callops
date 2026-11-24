# 데스크톱 AI 모듈

`app/analyzer.py`는 Qwen3 8B 구조화 분석, `app/transcriber.py`는 Whisper 전사 단독 실행 모듈이다. 사용자가 성공시킨 `test_whisper.py`의 large-v3-turbo / CUDA / int8_float16 / 한국어 / beam_size=5 설정을 유지한다. VAD는 기존 기본값인 false를 명시한다.

## 전사 실행 — WSL 데스크톱

이미 전사에 성공한 `callops-runtime/.venv`와 모델 캐시를 사용한다. 새 패키지 설치나 드라이버 변경은 하지 않는다. 모델은 로컬 캐시에서만 읽고, 캐시가 없거나 GPU 실행이 실패하면 오류로 종료한다.

개인정보 없는 테스트 음성을 명시적으로 선택해 실행한다. 입력·출력 파일은 Git 밖에서 관리하고, 출력 디렉터리는 해당 사용자만 접근하도록 한다.

```bash
cd ~/callops/ai-worker
source ~/callops-runtime/.venv/bin/activate
mkdir -p -m 700 ~/callops-runtime/transcripts
python -m app.transcriber \
  ~/callops-runtime/test.m4a \
  ~/callops-runtime/transcripts/test-whisper.json
```

이미 출력 파일이 있으면 덮어쓰지 않으므로 새 출력 파일명을 지정한다. 구간 이터레이터를 끝까지 소비하고 검증한 뒤 임시 파일을 fsync하고 같은 디렉터리의 최종 파일로 원자적으로 공개한다. WSL에서는 파일 권한 600을 사용하며, 하드 링크를 지원하는 파일시스템이 필요하다. 전사문·입력 파일명·원본 오류 내용은 CLI에서 출력하지 않는다. [faster-whisper 공식 사용 안내](https://github.com/SYSTRAN/faster-whisper#usage).

결과에는 모델 이름·연산 설정·언어·음성 길이·모델 로드를 포함한 처리시간과 `segments`의 `segment_id/start/end/text`가 들어간다. 잘못된 시간 범위·역순·비유한 값은 거부한다. 빈 전사는 `NO_SPEECH` 경고이며 무음 판정의 정확성을 보장하지 않는다. 최대 입력 길이는 2시간이지만 실제 최대 길이의 성능은 미검증이다.

## GPU 없이 코드 검사

저장소 루트에서 실행한다. 전사·분석 테스트는 `unittest.mock`으로 Whisper와 Ollama 호출을 대체한다. 분석 테스트에는 기존 스키마를 검증하는 Pydantic 2가 필요하다. GPU, 모델 파일, Ollama 설치는 필요 없다.

```text
python -m unittest discover -s ai-worker/tests -v
python scripts/check.py
```

분석만 검사하려면 `python -m unittest discover -s ai-worker/tests -p test_analyzer.py -v`를 실행한다. 분석 요청과 구조화 결과, 빈 전사, 잘못된 JSON 및 누락된 필드·근거를 검증한다. 서버 검증기에 문자열 요약을 보내면 거절되고, 합성 결과에 `summary.text`와 `summary.segment_ids`를 함께 보내면 형식 검사를 통과한다. 현재 분석 스키마는 요약을 문자열로 제공한다. [분석 모의 테스트](tests/test_analyzer.py) · [서버 요약 계약](../docs/p2-p3-server.md#합성-요약-결과-스키마와-조회)

2026-11-24 재확인: 시스템 Python에서 `python -m unittest discover -s ai-worker/tests`의 12개(전사 8·분석 4)가 통과했다. 서버용 `.venv`에는 Pydantic 2가 없어 분석 테스트가 가져오기 단계에서 실패하므로 Pydantic 2가 설치된 환경에서 실행한다.

2026-10-09 검증(당시 기록, 분석 테스트 추가 전): 전사 모듈 테스트 8개가 Windows Python 3.12와 WSL Python에서 통과했다. 기본 하네스 6개와 새 PostgreSQL 클러스터의 P1~P3 전체 29개도 통과했고 임시 클러스터를 종료·정리했다. Windows 샌드박스의 임시 경로·하드 링크 제한과 DB 준비 시간 초과 이후, 쓰기 가능한 임시 경로 및 샌드박스 밖 실행으로 재검증했다. 모델 호출은 Mock이며 새 전사 모듈의 실제 GPU 실행을 뜻하지 않는다.

## 구현 경계

이번 모듈은 수동 개발 시험용이다. 서버 큐에 연결하지 않으며 DB 승인·임대·취소 검사를 대신하지 않는다. 서버는 여전히 P3 합성 결과만 수락한다. 자동 폴더 감시나 실제 통화 수집을 실행하지 않는다.

화자 구분, Qwen 연결, 모델 digest와 패키지 버전 고정, job/attempt 입력 식별자, heartbeat·상한 중단·ACK·spool 재개는 후속 작업이다. GPU 메모리 해제·긴 통화·잡음·겹침·STT 품질 평가는 아직 완료하지 않았다. 소스에 음성·전사·런타임 환경을 복사하지 않는다.
