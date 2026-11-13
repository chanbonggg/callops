# 인증 모듈

## 책임과 구현 의도

개인용 서버의 소유자 세션과 워커 Bearer 인증을 한 곳에서 처리한다. 기능별 라우트는 인증 결과를 받아 각자의 상태·리소스 권한을 검증한다. 앱 조립, 쿠키 옵션, 신뢰 호스트와 공통 응답 헤더는 `web.py`에서 관리한다. [앱 조립](../web.py)

## 요청 흐름

- `GET /login`은 로그인용 CSRF 토큰을 만든다. 로그인 POST는 토큰과 scrypt 비밀번호 해시를 검증한다. 실패 횟수와 재시도 시각은 PostgreSQL에 저장한다.
- 로그인 성공 시 서명된 세션 쿠키에 토큰을 두고 DB에 토큰 해시와 CSRF, 만료 시각을 저장한다. 보호된 요청은 DB 세션을 확인해 `request.state.login`을 채운다. 로그아웃 POST는 DB 세션을 폐기한다.
- `/worker/`는 Bearer 토큰으로 워커를 식별해 `request.state.worker`를 채운다. 작업별 임대와 권한은 큐에서 확인한다. [워커 요청 흐름](../processing/AGENTS.md)

## 변경 시 확인

- 쿠키의 `Secure`, `HttpOnly`, `SameSite=Strict`와 DB 세션 만료 검사를 유지한다. 보호된 폼 변경은 `request.state.form`과 CSRF 검사를 사용한다. GET으로 승인·수정·삭제를 실행하지 않는다.
- 로그에 토큰·비밀번호·요청 폼 원문을 남기지 않는다. 본문 제한을 바꿀 때 브라우저 폼과 워커 파일 업로드 양쪽을 검사한다.
- 로그인 제한, 세션 폐기·만료, CSRF, multipart 경계는 P2에서 재현한다. [수집·인증 테스트](../../../tests/p2/test_ingestion.py)
