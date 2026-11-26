# 홈서버에서 CallOps 실행

## 접속

Tailscale을 연결하고 `https://<홈서버 VPN 호스트>:8443/login`을 연다. 브라우저에서 처음 접속하기 전에 서버의 `~/.config/callops/web-ca.crt` 공개 인증서를 SSH로 받아 사용자 신뢰 저장소에 등록한다. Windows PowerShell에서는 다음 명령을 사용한다.

```powershell
scp my-server:/home/chan/.config/callops/web-ca.crt .\callops-web-ca.crt
certutil -user -addstore Root .\callops-web-ca.crt
```

소유자 로그인에는 서버 설정 디렉터리에 보관한 계정을 사용한다. CallOps 웹 세션은 HTTPS를 사용하고, PostgreSQL은 Compose 내부 네트워크에서 TLS와 역할 인증으로 접속한다.

## 운영 경로

| 항목 | 서버 위치 |
|---|---|
| 소스와 Compose | `/home/chan/callops`, [Compose 구성](../deploy/server/compose.yaml) |
| 비밀값과 인증서 | `/home/chan/.config/callops` |
| CallOps 파일 | `/home/chan/.local/share/callops` |
| PostgreSQL | Docker 볼륨 `callops_database` |
| 웹과 보관 관리 | `callops-web-1`, `callops-maintenance-1` |

마이그레이션 컨테이너만 `migration.env`를 받는다. 웹과 보관 관리 컨테이너는 앱용 DB 역할을 사용한다. 보관 관리 컨테이너는 약 5분마다 기존 `maintain` 명령을 실행한다.

## 서버에서 적용 및 확인

기존 PostgreSQL 18 Alpine 이미지와 같은 기반에서 pgvector 0.8.7을 빌드한다. 운영 DB를 갱신할 때는 사용 중인 볼륨을 유지하고 기존 DB를 백업한다. 신규 DB는 초기화 단계에서 확장을 등록한다. 기존 DB는 DB 관리 계정으로 `callops` 스키마에 확장을 등록한 뒤 앱 마이그레이션을 실행한다.

```bash
cd /home/chan/callops
callops_compose() {
  docker compose --env-file /home/chan/.config/callops/compose.env -f deploy/server/compose.yaml "$@"
}
callops_compose config --quiet
callops_compose build db web
callops_compose up -d --wait --wait-timeout 90 db
callops_compose exec -T -u postgres db psql -U postgres -d callops -v ON_ERROR_STOP=1 \
  -c 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA callops'
callops_compose run --rm --no-deps migrate
callops_compose up -d --no-build --pull never --wait --wait-timeout 120 web maintenance
callops_compose ps
callops_compose logs --tail 20 maintenance
```

적용 전 `git status --short`로 서버 배포 설정을 확인한다. 웹 연결 문제가 생기면 Tailscale 상태와 사용 중인 주소를 확인하고, 운영 인증서 및 비밀값은 기존 설정 디렉터리에 보관한다. 이 배포의 접속·운영 검증 범위는 [진행 체크리스트](progress-checklist.md)에 기록한다.

VPN과 HTTPS로 최신 데스크톱 워커를 연결한 시험에서 준비 응답과 결과 수락을 확인한다. 모델 질문을 켤 때 `web.env`에 지정 워커 ID인 `CALLOPS_REAL_WORKER_ID`와 `CALLOPS_ENABLE_RAG=1`을 설정한다. [서버 질문 흐름](p2-p3-server.md#pgvector와-질문-워커)
