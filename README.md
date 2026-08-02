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

## 6. 주간 Claude 분석 (dart_weekly_analysis_workflow.json)
- 매주 월 09:00 CT: predictions+actuals 조인 → 지표 계산(Code 노드, 결정적) → Claude API가 해석 작성 → `analysis` 탭 append
- 시트에 `analysis` 탭 추가 필요. 헤더: week_ending, n_hours_7d, hit_7d, save_vs_half_7d, save_vs_rt_7d, hit_cum, save_vs_rt_cum, worst_hour_7d, claude_analysis
- 크리덴셜: HTTP Header Auth — name `x-api-key`, value = Anthropic API 키. 모델명(claude-sonnet-4-5)은 HTTP 노드 body에서 변경 가능
- 판정 기준 리마인드: vs항상RT 7일 절감이 2주 연속 음수면 모델 재검증 트리거

## 7. runlog 탭 (진단)
매 실행마다 무엇을 처리/건너뛰었는지 기록됩니다. 시트에 `runlog` 탭 추가 필요.
헤더: run_at, target_day, todo_days, processed, skipped, backfilled, state_rows_returned, state_days_in_sheet
- 헤더 끝에 `model_detail_rows` 추가 (v9)
- `skipped`의 사유 예: `load_fcst[2026-07-26:없음,2026-07-27:행0,...]` → 어느 상품이 어느 발행일에서 실패했는지 표시
- 백필은 MIS 7일 보관 한도 내에서만 가능. 그 밖의 날짜는 영구적으로 복원 불가(정상)

## 8. model_detail 탭 (개별 모델 실행 결과) — v9
`predictions` 탭은 **앙상블 평균 후** 최종 판정만 담습니다. 평균 내기 전 개별 모델(시드별) 출력을
그대로 보려면 시트에 `model_detail` 탭을 추가하세요. 시간당 1행, 34+1개 컬럼.

헤더 (순서대로 1행에 붙여넣기):
```
dt_local	ens_e_dart	ens_decision	ens_p_spike	m3_7_p_da	m3_7_p_rt	m3_7_e_dart	m3_7_decision	ms_7_p_spike	m3_17_p_da	m3_17_p_rt	m3_17_e_dart	m3_17_decision	ms_17_p_spike	m3_42_p_da	m3_42_p_rt	m3_42_e_dart	m3_42_decision	ms_42_p_spike	m3_101_p_da	m3_101_p_rt	m3_101_e_dart	m3_101_decision	ms_101_p_spike	m3_202_p_da	m3_202_p_rt	m3_202_e_dart	m3_202_decision	ms_202_p_spike	agree_n	e_dart_std	e_dart_min	e_dart_max	p_spike_std	generated_at
```

컬럼 의미
- `m3_<시드>_*` : 3-클래스 모델 5개(시드 7/17/42/101/202) 각각의 원본 출력.
  `p_da`=DA가 쌀 확률, `p_rt`=RT가 쌀 확률 (p_neutral = 1 − p_da − p_rt),
  `e_dart`=그 모델 단독의 기대 스프레드, `decision`=그 모델 **혼자 판정했다면** 나왔을 결론
- `ms_<시드>_p_spike` : 스파이크 모델 5개 각각의 P(DART < −$10) (상대 랭킹용, 보정 확률 아님)
- `ens_*` : 실제 운용에 쓰이는 앙상블 값 (predictions 탭과 동일)
- `agree_n` : 5개 모델 중 앙상블 판정과 같은 결론을 낸 개수 (5=만장일치, 3 이하=의견 갈림)
- `e_dart_std / min / max` : 모델 간 기대값 분산 — 클수록 그 시간대 판정 신뢰도가 낮음
- `p_spike_std` : 스파이크 모델 간 분산

활용법
- `agree_n <= 3` 인 시간대는 사실상 "모델도 확신이 없는 구간" → 실무에서 중립 취급 검토
- `e_dart_std` 가 밴드($0.5)보다 크면 앙상블 판정이 시드 운에 좌우된 것 → 주간 리뷰에서 별도 집계
- 주간 분석 시 시드별 `decision` vs 실제 `dart` 로 **어떤 시드가 계속 틀리는지** 추적 가능
  → 특정 시드가 지속 열위면 다음 재학습에서 교체 후보

일일 요약(Claude)에도 `lowAgree`(agree_n≤3 시간 수), `avgStd`, `maxStd` 가 자동으로 전달됩니다.
