# 데스크톱 AI 모듈

`app/analyzer.py`는 Qwen3 8B 구조화 분석, `app/transcriber.py`는 Whisper 전사 단독 실행 모듈이다. D3에서 선택한 로컬 구성은 `large-v3`, CUDA `int8_float16`, 한국어, beam size 5다. 음성 감지와 단어 단위 시간 정렬을 켜고 첫 프롬프트로 한국어 업무 통화임을 알린다. 이전 디코딩 구간의 텍스트는 다음 구간에 넘기지 않는다. [D3 검증 결과](../docs/02-desktop.md#2026-11-23-d3-화자-구분과-전사-재검증)

## 전사 실행 — WSL 데스크톱

기존 `callops-runtime/.venv`에서 실행한다. CLI는 기본으로 `~/callops-runtime/d3/models/large-v3`에 보관한 모델을 사용한다. `--model`로 캐시된 모델 이름이나 로컬 디렉터리를 직접 지정할 수 있다. 모델을 찾을 수 없거나 GPU 실행에 실패하면 오류로 종료한다.

개인정보 없는 테스트 음성을 명시적으로 선택해 실행한다. 입력·출력 파일은 Git 밖에서 관리하고, 출력 디렉터리는 해당 사용자만 접근하도록 한다.

```bash
cd ~/callops/ai-worker
source ~/callops-runtime/.venv/bin/activate
callops_gpu_libs="$VIRTUAL_ENV/lib/python3.12/site-packages/nvidia"
export LD_LIBRARY_PATH="$callops_gpu_libs/cublas/lib:$callops_gpu_libs/cudnn/lib:/usr/lib/wsl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p -m 700 ~/callops-runtime/transcripts
python -m app.transcriber \
  ~/callops-runtime/test.m4a \
  ~/callops-runtime/transcripts/test-whisper.json
```

이미 출력 파일이 있으면 덮어쓰지 않으므로 새 출력 파일명을 지정한다. 구간 이터레이터를 끝까지 소비하고 검증한 뒤 임시 파일을 fsync하고 같은 디렉터리의 최종 파일로 원자적으로 공개한다. WSL에서는 파일 권한 600을 사용하며, 하드 링크를 지원하는 파일시스템이 필요하다. 전사문·입력 파일명·원본 오류 내용은 CLI에서 출력하지 않는다. [faster-whisper 공식 사용 안내](https://github.com/SYSTRAN/faster-whisper#usage).

결과에는 모델 이름·연산 및 시간 정렬 설정·언어·음성 길이·처리 시간과 `segments`의 `segment_id/start/end/text`가 들어간다. 잘못된 시간 범위·역순·비유한 값은 거부한다. 빈 전사는 `NO_SPEECH`로 표시한다. 20ms 구간 에너지의 90·20백분위 차가 15dB 이하이면 `LOW_AUDIO_CONTRAST`, 원본이 협대역 AMR이면 `NARROWBAND_AMR`를 기록해 검토를 요청한다. 최대 입력 제한은 2시간이며 D3 장문 실측은 약 9분이다. Ctrl+C나 SIGTERM으로 저장 도중 취소되면 임시 결과를 지우고 종료 코드 130을 반환한다.

화자가 두 명임을 아는 시험 음성에는 아래 옵션을 사용한다. `speaker_turns`는 sherpa-onnx가 추정한 원본 시각 구간이다. `segment_speakers`는 전사 구간 안에서 한 화자만 감지되고 그 화자의 구간이 절반보다 길 때 A/B를 붙인다. 복수 화자나 근거가 부족하면 `null`과 `SPEAKER_UNCERTAIN`을 반환한다. 감지된 동시 발화에는 `SPEECH_OVERLAP`을 표시한다. A/B는 한 파일 안에서만 사용하는 라벨이다.

```bash
PYTHONPATH="$HOME/callops-runtime/d3/python${PYTHONPATH:+:$PYTHONPATH}" \
  python -m app.transcriber \
  ~/callops-runtime/test-two-speakers.wav \
  ~/callops-runtime/transcripts/test-speakers.json \
  --two-speaker-models \
  "$HOME/callops-runtime/d3/models/pyannote-segmentation-3.0.onnx" \
  "$HOME/callops-runtime/d3/models/3dspeaker-eres2net.onnx"
```

## GPU 없이 코드 검사

저장소 루트에서 D1 Python 환경으로 실행한다. 전사·분석 테스트는 `unittest.mock`으로 Whisper와 Ollama 호출을 대체한다. Python 의존성은 [D1 패키지 스냅샷](requirements-d1.txt)을 참고한다.

```text
python -m unittest discover -s ai-worker/tests -v
python scripts/check.py
```

분석만 검사하려면 `python -m unittest discover -s ai-worker/tests -p test_analyzer.py -v`를 실행한다. 빈 입력, 응답 완료 여부, 필수 필드, 근거 ID와 원문 표현, 조각 전체 처리, 날짜·시간 경계와 담당자를 검증한다. D4의 `summary.text`와 `summary.segment_ids`는 서버의 합성 요약 필드와 같은 형태다. [분석 모의 테스트](tests/test_analyzer.py) · [서버 요약 계약](../docs/p2-p3-server.md#합성-요약-결과-스키마와-조회)

2026-11-24에 서버 브랜치의 모의 검사 12개를 시스템 Python에서 실행했다. 2026-11-25에는 D1 환경에서 전사·화자 구분·분석·검색 모의 검사 46개가 통과했다.

2026-10-09 검증(당시 기록, 분석 테스트 추가 전): 전사 모듈 테스트 8개가 Windows Python 3.12와 WSL Python에서 통과했다. 기본 하네스 6개와 새 PostgreSQL 클러스터의 P1~P3 전체 29개도 통과했고 임시 클러스터를 종료·정리했다. Windows 샌드박스의 임시 경로·하드 링크 제한과 DB 준비 시간 초과 이후, 쓰기 가능한 임시 경로 및 샌드박스 밖 실행으로 재검증했다. 모델 호출은 Mock이며 새 전사 모듈의 실제 GPU 실행을 뜻하지 않는다.

## D4 정답 전사 평가 — WSL 데스크톱

전용 시험 Ollama를 WSL의 루프백에 띄운다. 다음 명령의 모델 경로는 WSL 서비스의 기존 캐시다. 다른 위치에 설치했으면 기존 캐시 경로를 사용한다. 시험 서버를 시작한 셸에 로그가 남으며 종료는 Ctrl+C로 한다.

```bash
umask 077
callops_d4_dir=$(mktemp -d)
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NO_CLOUD=1 \
  OLLAMA_MODELS=/usr/share/ollama/.ollama/models OLLAMA_NUM_PARALLEL=1 \
  OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=8192 \
  ollama serve 2>&1 | tee "$callops_d4_dir/ollama.log"
```

시작 로그에서 `Ollama cloud disabled: true`와 `Listening on 127.0.0.1:11435`를 확인한 뒤 다른 셸에서 실행한다.

```bash
cd ~/callops/ai-worker
source ~/callops-runtime/.venv/bin/activate
OLLAMA_HOST=127.0.0.1:11435 ollama list
python -m app.evaluate_analysis T01 T06 T09 --port 11435
OLLAMA_HOST=127.0.0.1:11435 ollama ps
/usr/lib/wsl/lib/nvidia-smi --query-gpu=memory.used --format=csv
```

이 명령은 T01, T06, T09 순서로 평가하며 T06과 T09는 각각 한 조각과 발화별 분할을 시험한다. JSONL 첫 줄에는 Ollama 버전, 모델 digest·양자화, 프롬프트·스키마 버전과 해시, 생성 설정이 들어간다. 이후 줄은 각 시험의 기대값, 실제 후보·근거, 응답 종료 사유와 토큰 수, 요청별 원본 범위와 시간을 담는다. 시험에 실패하면 종료 코드 1을 반환한다. 실패 응답에는 합성 대본만 포함된다.

고정 세트는 같은 설정에서 `--repeat 3`으로 실행한다. `--mode long`의 T06/T09는 원본 약속과 정정·취소 사이에 합성 현황 발화를 넣어 바이트 예산에 따른 분할을 시험한다. [대본과 판정 기준](../docs/02-desktop.md#6-d4--ollama-추출-단독-검증) · [추가 화자와 날짜 대본](../tests/fixtures/d4-analysis-edges.json)

```bash
python -m app.evaluate_analysis T01 T06 T09 T02 T03 T05 T07 T08 T10 \
  D4-TASK D4-SPEAKER D4-NO-CLOCK D4-OVERNIGHT --port 11435 --repeat 3
python -m app.evaluate_analysis T06 T09 --mode long --port 11435 --repeat 3
```

## D4 검색 평가

클라우드를 끈 시험 Ollama의 캐시에 `bge-m3:567m`과 추출에 사용한 `qwen3:8b`를 준비한다. BGE-M3가 없으면 쓰기 가능한 시험 캐시를 사용하는 서버에 `OLLAMA_HOST=127.0.0.1:11435 ollama pull bge-m3:567m`으로 받아 둔다. 개발 세트에서 검색 조각 크기를 비교할 수 있다. 최종 명령은 개발 보고서의 모델 digest·평가 및 공용 모델 코드·프롬프트·스키마·검색 설정이 현재 실행과 일치하는지 확인한다. 최종 평가 전 현재 코드로 개발 보고서를 생성한다. [Ollama BGE-M3](https://ollama.com/library/bge-m3:567m)

```bash
umask 077
callops_rag_dir=$(mktemp -d)
python -m app.evaluate_rag development --window-segments 2 --port 11435 \
  > "$callops_rag_dir/development.jsonl"
python -m app.evaluate_rag final --window-segments 2 --port 11435 \
  --development-report "$callops_rag_dir/development.jsonl" > "$callops_rag_dir/final.jsonl"
```

상대와 한국 날짜로 좁힌 합성 통화에서 찾은 발화를 Qwen이 읽고 초안의 사실과 인용을 한 번 더 대조한다. 초안의 인용 ID가 유효하지 않으면 검색된 ID만 허용해 한 번 재시도한다. 합의된 수치를 묻고 원문에 미확정 표현이 있으면 답변 가능 여부를 먼저 판단하고, 보류 사유에는 근거 발화를 직접 인용한다.

JSONL에서 답변 가능 문항의 필요한 ID가 상위 5개 조각에 모두 들었는지 확인한다. 각 답변의 시각·물량·주체·최종 합의와 보류 사유는 근거에 직접 대조한다. [웹 MVP 평가 기준](../docs/web-mvp/README.md#week-05-평가-기준) · [첫 고정 평가](../docs/02-desktop.md#2026-11-24-d4-검색-평가) · [재평가 결과](../docs/02-desktop.md#2026-11-25-d4-검색-재평가)

## 질문 모델 워커 실행

`StageProcess.run()`이 기존 D5 프로세스 그룹에서 `RAG_EMBED`와 `RAG_ANSWER`를 실행한다. 호출자는 같은 워커 루트의 잠금을 잡고, 관리 작업 디렉터리의 `input`에 UTF-8 JSON을 둔다. 실행 기한과 취소를 검사하는 함수를 넘기며, 성공 결과는 `result.json`에 원자적으로 기록된다. 호출자가 결과 사용 후 작업 사본을 청소하고, 엔진 종료를 확인하지 못한 경우에는 기존 `engine-active` 복구 절차를 따른다. 입력과 결과 파일을 사용자 전용으로 관리한다.

| 실행 단계 | 입력 | 결과 |
| --- | --- | --- |
| `RAG_EMBED` | `schema_version: 1`, `texts` 1~16개. 공백 제거 후 항목마다 UTF-8 4KiB까지 받는다. 질문과 전사 조각은 같은 모델에 입력한다. | `schema_version`, `model_name`, `model_digest`, `dimensions`(1024), `embeddings`. 입력 순서대로 유한한 단위 벡터를 반환한다. 잘린 입력이나 일부 벡터만 성공한 배치는 실패한다. |
| `RAG_ANSWER` | `schema_version: 1`, `question` 1~200자, 선택 `scope`, 검색된 `evidence` 0~10개. `scope`의 `counterparty`는 최대 100자이고 `from_date`·`to_date`는 `YYYY-MM-DD`다. | `schema_version`, `model_name`, `model_digest`, `prompt_version`, `answer`, `reason`, `segment_ids`. 답변과 보류 사유 중 하나만 채운다. 근거가 비면 모델을 시작하지 않고 모델명·digest가 `null`인 보류를 반환한다. |

각 근거에 요청 내 고유 `id`, `call_id`, 원음 초 단위 `start`·`end`, `text`를 담는다. `id`와 `call_id`에는 영문·숫자·`_`·`-`·`:`를 최대 80자까지 사용한다. `speaker`는 `A`·`B`·`null`이다. `call_started_at`은 신뢰할 수 있는 오프셋 포함 ISO 8601 시각 또는 `null`이며 모델에는 한국 시각으로 전달한다. 겹치는 검색 조각의 근거는 호출자가 ID로 중복 제거한다. 예를 들어 해당 전사 버전과 구간에 매핑할 수 있는 `artifact-id:12`를 사용할 수 있다.

두 단계는 80KiB를 넘는 JSON과 미정의 필드를 거부한다. 답변은 D4의 `rag-v10` 초안·인용 검토·미확정 수치 판단을 사용한다. 시스템 프롬프트와 JSON이 7168바이트를 넘으면 해당 모델 요청은 실패한다. 서버 연동에서는 이 경우를 일반 모델 실패와 구분해 `RAG_CONTEXT_TOO_LONG`으로 보고하며 자동 재시도하지 않는다. [서버·워커 계약](../docs/rag-worker-contract.md#답변-한도와-보관) 인용의 모든 ID가 해당 요청의 근거에 있어야 하며, 스키마 오류나 모델 실패·시간 초과 시 결과를 수락하지 않는다. 근거가 부족한 보류는 성공 응답으로 처리한다. 서버가 업무 승인·권한·고정 전사 버전·삭제·만료를 확정한 범위의 발화만 넘기는 계약이며, 결과 표시 전에도 서버에서 근거 연결과 유효성을 재검사해야 한다. 이 데스크톱 경로를 서버의 pull 큐와 질문 화면에서 호출하는 작업은 [웹 MVP 4단계](../docs/web-mvp/README.md#week-04)에서 진행한다.

### WSL GPU 합성 시험

D1 Python 환경과 D4에서 확인한 `bge-m3:567m`·`qwen3:8b` 캐시를 사용한다. 기본 캐시에 모델이 없으면 D4 절차로 준비한 시험 캐시를 `--ollama-models`에 지정한다. 워커가 단계별로 전용 루프백 Ollama를 시작하고 클라우드를 끈다. 모델 digest는 D4 값과 일치해야 하고, 결과 반환 전에 실행 모델의 GPU 적재를 확인한다. 시험할 때는 다른 GPU 작업이 끝난 상태에서 실행한다.

```bash
cd ~/callops/ai-worker
source ~/callops-runtime/.venv/bin/activate
callops_rag_root=$(mktemp -d)
python -m app.rag_smoke --root "$callops_rag_root" \
  --ollama-models /usr/share/ollama/.ollama/models
```

`RAG_EMBED_OK`는 입력 두 개의 1024차원 벡터를, `RAG_ANSWER_OK`는 합성 약속의 날짜·시각·장소와 제안·수락 근거 두 개를 확인했다는 뜻이다. 시험은 단계별 120초를 적용하며 복사본을 청소한 뒤 상태만 출력한다. 워커 회귀 검사는 저장소 루트에서 `python -m unittest discover -s ai-worker/tests -p test_rag_worker.py -v`로 실행한다. [범위와 완료 기준](../docs/02-desktop.md#질문-모델의-데스크톱-워커-실행)

## D5 워커 실행 — WSL 데스크톱

합성 음성으로 시험할 별도 PostgreSQL과 로컬 HTTPS 서버를 준비한다. 시험 서버 프로세스의 `CALLOPS_REAL_WORKER_ID`에 등록된 워커 ID를 지정한다. 기본 ID는 `desktop`이다. 서버와 데스크톱에는 동일한 전용 시험 토큰을 환경 변수 `CALLOPS_WORKER_TOKEN`으로 설정한다. [서버 설정과 결과 계약](../docs/p2-p3-server.md#d5-실제-모델-결과)

```bash
cd ~/callops/ai-worker
source ~/callops-runtime/.venv/bin/activate
callops_gpu_libs="$VIRTUAL_ENV/lib/python3.12/site-packages/nvidia"
export LD_LIBRARY_PATH="$callops_gpu_libs/cublas/lib:$callops_gpu_libs/cudnn/lib:/usr/lib/wsl/lib"
python -m app.worker --once \
  --server https://127.0.0.1:8443 --ca-file /tmp/callops-d5-test/server.crt \
  --root ~/callops-runtime/d5-test \
  --whisper-model ~/callops-runtime/d3/models/large-v3 \
  --ollama-models /usr/share/ollama/.ollama/models
```

서버에 사용한 시험 인증서와 실제 로컬 모델 경로를 지정한다. 워커 루트의 상위 디렉터리는 미리 만든다. 워커는 사용자 전용 `work`와 `spool`을 만들고, 분석마다 전용 루프백 Ollama 프로세스를 실행한다. `--two-speaker-models <분할 모델> <임베딩 모델>`은 큐에 넣는 합성 음성이 두 화자로 확인된 시험에서 사용한다. 모델 파일은 [전사 실행](#전사-실행--wsl-데스크톱)과 같다.

시험 서버에 [완료 체크섬으로 합성 오디오를 수신](../docs/p2-p3-server.md#수집분류)하고, 로그인해 통화 시각을 입력한 뒤 업무로 지정한다. `--once`를 두 번 실행하면 Whisper 결과를 저장·청소한 뒤 서버 전사를 다시 받아 Qwen으로 분석한다. 이전 실행에서 업로드 확인이 남아 있으면 서버 상태를 먼저 조회한다. 연속 시험에서는 `--once`를 생략한다. `RESULT_ACCEPTED`는 서버 수락과 데스크톱 청소 확인 후 출력된다.

임대·단계 기한이 끝나거나 업무 승인이 취소되면 워커가 자기 연산 프로세스 그룹을 중단한다. 종료를 확인할 수 없거나 실행 중 워커가 강제 종료되면 `engine-active` 기록이 남고 같은 루트에서 새 작업을 차단한다. 이 상태의 작업은 소유한 모델 프로세스의 종료를 확인한 뒤 복구한다. [D5 검증 기록](../docs/02-desktop.md#2026-11-25-d5-로컬-워커-검증)
