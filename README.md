# ERCOT DART 3-Class Scorer — 배포 가이드

## 구성
n8n(스케줄 08:30 CPT + Sheets I/O) → Render(FastAPI 스코어러, 무상태) → Google Sheets "ERCOT DART Tracker"

## 1. Render 배포
1. 이 폴더(deploy/)를 GitHub 리포로 푸시 (models/ 포함, 총 ~20MB)
2. Render → New Web Service → 해당 리포 연결
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - 환경변수: `EIA_API_KEY` = (EIA v2 키)
3. 확인: `GET https://<your-app>.onrender.com/health`
   - 주의: Render 무료 플랜은 슬립 → n8n HTTP 노드 타임아웃을 120s+로, 또는 08:25에 /health 웜업 호출 추가

## 2. Google Sheet ("ERCOT DART Tracker", 생성됨)
탭 3개 구성:
- `predictions` (기본 탭 이름 변경): dt_local, p_da_cheap, p_neutral, p_rt_cheap, p_spike, e_dart, decision, generated_at
- `state`: bootstrap_state.csv를 임포트 (헤더 포함). 컬럼: dt_local, lf_sys, stwpf, wgrpp, stppf, temp_fcst, temp_act, wind, solar, outage_total, outage_houston, outage_irr
  (temp_act/wind/solar 컬럼은 앱이 사용하지 않으므로 있어도 무방)
- `actuals`: dt_local, da_spp, rt_spp, dart

## 3. n8n 워크플로 (dart_daily_workflow.json 임포트)
흐름: Schedule(08:30 America/Chicago) → [웜업 /health] → Sheets Read(state 탭 전체) →
HTTP POST `<render>/score` (body: {"state": rows}) → 분기 3개:
  predictions[] → Sheets Append(predictions)
  new_state[] → Sheets Append(state)
  settlements[] → Sheets Append(actuals)  ※ 중복 append 가능 — 시트에서 dt_local 기준 중복 제거하거나 n8n에서 기존 actuals 마지막 dt 이후만 필터
크리덴셜: 기존 Google Sheets 자격증명 연결, 시트 ID = 1_8iT4KB0SG6gra56E2PyLvkr6rsjBJ_w6GkSUOeWUU4

## 4. 모델/규칙 (config.json)
- 5-시드 LGBM 앙상블 × (3-클래스, 스파이크) 10개 모델, 학습기간 ~2026-07-25
- 결정: E[DART] = p·μ (μ=[-12.83, 0.03, 10.80]) → < -0.5 → BUY_DA / > +0.5 → BUY_RT / else NEUTRAL_5050
- 스파이크 필터 기본 off (p_spike는 기록만 — 모니터링/재검증용)
- 기대 성능 (post-RTC+B, honest): vs 항상RT +$0.3~0.4/MWh, vs 반반 +$1.9~2.0/MWh

## 5. 운영 수칙
- **주간**: predictions vs actuals로 적중률·절감 리포트 (중립콜 적중 = |dart|≤$1)
- **월간**: 최근 데이터로 모델 재학습 권장 (이 세션 파이프라인 재실행 또는 Claude 세션에서 "모델 재학습" 요청)
- state 탭은 append-only — 롤링 계산에 최근 16일만 사용하므로 방치해도 무방
- 게이트 체크: 판정은 반드시 10:00 CPT (DAM 마감) 전에 사용
