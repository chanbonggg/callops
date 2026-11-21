# 데스크톱 AI 모듈

## 책임과 구현 의도

데스크톱에서 로컬 Whisper 전사와 Qwen 구조화 분석을 개발한다. 전사와 분석은 단독 실행 단계에 있다. 서버 큐 계약은 처리 모듈의 가짜 워커가 검증한다. [실행 안내](README.md) · [서버 처리 지침](../src/callops/processing/AGENTS.md)

## 실행 흐름

- `app/transcriber.py`는 로컬 캐시의 `large-v3-turbo`를 CUDA와 `int8_float16`으로 실행한다. 한국어·beam size 5·VAD 비활성은 기존 실험에서 사용한 설정이다. 입력 파일을 명시하고 전체 구간을 검증한 뒤 Git 밖에 JSON을 저장한다.
- 전사 출력은 같은 디렉터리에서 임시 파일을 fsync한 후 하드 링크로 공개한다. 기존 출력은 덮어쓰지 않고 CLI 오류에는 원문·파일명·디코더 상세를 노출하지 않는다.
- `app/analyzer.py`는 전사 텍스트와 프롬프트를 로컬 Ollama에 전달하고 `app/schemas.py`의 `CallAnalysis`로 결과 구조를 검증한다. 모델 출력의 근거 검증과 서버 승인·전송 권한은 서버가 담당한다.

## 변경 시 확인

- 실제 모델 호출을 기본 검사에 넣지 않는다. 모델 캐시·드라이버·패키지·데스크톱 연결 변경은 해당 환경의 실행 계획을 따른다. [데스크톱 계획](../docs/02-desktop.md)
- 루트에서 `python -m unittest discover -s ai-worker/tests -v`로 전사 생성·시간 검증·원자적 저장과 분석 요청·응답 스키마를 검사한다. 분석 테스트에는 Pydantic 2가 필요하며 Ollama 호출을 모의 처리한다. 실제 GPU 품질·서버 큐 통합 검증은 별도 단계다. [전사 모의 테스트](tests/test_transcriber.py) · [분석 모의 테스트](tests/test_analyzer.py)
