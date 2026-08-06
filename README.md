# ERCOT DART 3-Class Scorer — 배포 가이드

## 0. 배포 주소 (고정)
```
Render  : https://power-model.onrender.com
  /health            헬스체크·웜업
  /score   (POST)    일일 판정 + 백필
  /probe?days=7      백필 가능일 진단
  /peek?product=&pub= 원본 CSV 확인
Sheet ID: 1_8iT4KB0SG6gra56E2PyLvkr6rsjBJ_w6GkSUOeWUU4
```
워크플로 JSON에 이 주소가 하드코딩되어 있으므로 임포트 후 URL 수정 불필요.

## 구성
n8n(스케줄 08:30 CPT + Sheets I/O) → Render(FastAPI 스코어러, 무상태) → Google Sheets "ERCOT DART Tracker"

## 1. Render 배포
1. 이 폴더(deploy/)를 GitHub 리포로 푸시 (models/ 포함, 총 ~20MB)
2. Render → New Web Service → 해당 리포 연결
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - 환경변수: `EIA_API_KEY` = (EIA v2 키)
3. 확인: `GET https://power-model.onrender.com/health`  (현재 배포 주소)
   - 주의: Render 무료 플랜은 슬립 → n8n HTTP 노드 타임아웃을 120s+로, 또는 08:25에 /health 웜업 호출 추가

## 2. Google Sheet ("ERCOT DART Tracker", 생성됨)
탭 3개 구성:
- `predictions` (기본 탭 이름 변경): dt_local, p_da_cheap, p_neutral, p_rt_cheap, p_spike, e_dart, decision, generated_at
- `state`: bootstrap_state.csv를 임포트 (헤더 포함). 컬럼: dt_local, lf_sys, stwpf, wgrpp, stppf, temp_fcst, temp_act, wind, solar, outage_total, outage_houston, outage_irr
  (temp_act/wind/solar 컬럼은 앱이 사용하지 않으므로 있어도 무방)
- `actuals`: dt_local, da_spp, rt_spp, dart

## 3. n8n 워크플로 (dart_daily_workflow.json 임포트)
흐름: Schedule(08:30 America/Chicago) → 웜업 `/health` → Sheets batchGet 1회 →
HTTP POST `https://power-model.onrender.com/score` (body: {"state": rows}) → 분기 3개:
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

## 9. Sheets 쿼터 초과 대응 — 워크플로 v9 (배치 Append)

**증상**: `Quota exceeded for quota metric 'Read requests' ... sheets.googleapis.com`
**원인**: n8n Google Sheets Append 노드는 **입력 아이템(=행)마다** 헤더를 읽는다.
백필로 predictions 168행 + model_detail 168행 + state 168행이 나가면 읽기 요청이 수백 회 →
분당 60회(사용자당) 한도 초과. 행이 많을수록 반드시 재발한다.

**해결**: Append를 Sheets REST API 배치 호출로 교체. 노드 6쌍(Split×6 + Append×6)을 3개로 통합.

| | 기존 v8 | v9 |
|---|---|---|
| 쓰기 관련 API 호출 | 행 수만큼 (수백) | **헤더읽기 1 + 탭당 1 = 최대 7** |
| 노드 수 | 24 | 15 |

새 노드 3개
- `Read headers (1 call)` — `values:batchGet` 로 6개 탭 헤더를 **한 번에** 읽음
- `Build appends` — /score 결과와 Claude 요약을 **시트에 실제로 있는 헤더 순서대로** 2차원 배열로 패킹.
  시트에 없는 컬럼은 무시되고, 값이 없는 컬럼은 빈칸이 된다 (헤더 순서를 바꿔도 안전)
- `Append rows (batch)` — `values:append` 를 탭당 1회 POST. 재시도 3회, 호출 간 1.5초 간격

### 설정 시 주의
1. 두 HTTP 노드의 인증은 **Predefined Credential Type → Google Sheets OAuth2 API**로 잡고
   기존 Google Sheets 자격증명을 그대로 선택하면 된다 (새 크리덴셜 불필요).
2. **6개 탭이 모두 존재해야 한다** — `predictions, model_detail, state, actuals, daily_summary, runlog`.
   `batchGet`은 탭 하나라도 없으면 요청 전체가 400으로 실패한다. 없는 탭은 헤더만이라도 만들어 둘 것.
3. `daily_summary` 권장 헤더:
   `run_at, target_day, n_da, n_neutral, n_rt, max_p_spike, low_agree_hours, avg_e_dart_std, backfilled, claude_summary`
4. Claude 요약 노드는 실패해도 계속 진행하도록(`continueRegularOutput`) 바뀌었다 —
   Anthropic API가 죽어도 predictions/state/actuals 적재는 정상 수행된다.

### 그래도 쿼터가 나면
상단 읽기 노드 3개(`Read state tab`, `Read predictions (days)`, `Read actuals (days)`)가 남아 있다.
`Read state tab`은 append-only라 계속 커지므로, 6개월쯤 뒤 state 탭이 수천 행이 되면
탭을 최근 30일만 남기고 잘라내면 된다 (모델은 최근 16일만 사용).

## 10. /probe — 예측이 비었을 때 첫 번째로 볼 것 (v10)

`predictions[]`가 비면 원인은 **항상** `/score` 응답의 `meta.skipped_days` 에 문자열로 들어 있다.
내일치는 코드상 무조건 처리 대상이므로, 비었다는 건 그날이 skip 됐다는 뜻뿐이다.

n8n을 열 필요 없이 브라우저에서 바로 확인:
```
https://power-model.onrender.com/probe
https://power-model.onrender.com/probe?day=2026-08-02
```
state·날씨 없이 ERCOT MIS만 조회해서 상품 4종(load_fcst, wind, solar, outage)이
그 딜리버리일에 대해 잡히는지, 어느 발행일에서 잡혔는지, 안 잡혔으면 왜인지 보여준다.

읽는 법
- `all_ok: true` → MIS는 정상. 원인은 state 부족 / 날씨 / 시트 쪽
- `all_ok: false` → `reason`이 직접 원인. 예 `wind[2026-08-01:없음,2026-08-02:없음,...]`
  = 08:30 실행 시점에 그 상품이 아직 미발행. 스케줄을 09:30으로 늦추면 해결되는 경우가 많다
- `mis_publish_dates_available` = MIS가 현재 보관 중인 발행일(7일). 여기 없는 날짜는 복원 불가(정상)

### n8n 쪽 점검 순서
1. `POST /score` 노드 출력에서 `meta.skipped_days` / `meta.todo_days` 확인 — Split이 비는 건 결과이지 원인이 아님
2. 워크플로 v9를 임포트했다면 Split 노드는 존재하지 않는다. 화면에 Split이 보이면 아직 v8이므로 v9로 교체
3. 그래도 비면 `/probe` 결과를 그대로 공유

## 11. 백필이 안 되던 진짜 원인 — MIS 날짜 파싱 (v11에서 수정)

`/probe` 실측 결과 (2026-08-02 00:44 CT 실행):
```
load_fcst[2026-08-02:날짜파싱실패, 2026-08-03:없음, 2026-08-01:날짜파싱실패, ...]
outage   [2026-08-02:날짜파싱실패, ...]
```
문서는 **있는데** 읽고 나서 날짜 컬럼 파싱에서 깨지고 있었다.
v10까지는 `pd.to_datetime(df[col], format="%m/%d/%Y")` 로 **한 가지 포맷만** 시도했고,
포맷이 안 맞으면 예외 → 그 발행일을 통째로 스킵 → 후보 4개 전부 실패 → 그날 예측 없음.
"내일치도 없고 백필도 안 되던" 증상이 전부 이 한 줄에서 나왔다.

v11 수정
- `_parse_dates()` — `MM/DD/YYYY` → `YYYY-MM-DD` → 자동추론 순으로 시도, 전부 NaT일 때만 실패 처리
- `_c()` — 컬럼명 대소문자·언더스코어·공백 차이를 흡수 (`DELIVERY_DATE` == `DeliveryDate`)
- `_hours()` — `01:00` / `1` / `24:00` / 정수 모두 허용
- `mis_read_csv()` — zip 안에서 `.csv`를 골라 읽고 컬럼명 공백 제거
- 실패 사유가 구체화됨: `날짜파싱실패(col=DeliveryDate,예='2026-08-03')`,
  `행0(문서범위 2026-08-01~2026-08-07)` — 문서에 어느 날짜들이 들어있는지까지 표시

### 백필 확인 방법
```
https://power-model.onrender.com/probe?days=7
```
→ `recoverable_days` 에 나온 날짜만 백필 가능. MIS 보관 7일 밖은 영구 복원 불가(정상).

### 그래도 파싱이 실패하면
```
https://power-model.onrender.com/peek?product=wind&pub=2026-08-01
```
원본 CSV의 컬럼명 전체, 날짜 컬럼 샘플값, 문서에 들어있는 딜리버리일 목록, 샘플 2행을 그대로 보여준다.
product: load_fcst | wind | solar | outage | dam_spp_daily | rtm_spp_daily

## 12. 진짜 근본원인 — XML 문서를 CSV로 읽고 있었다 (v12에서 수정)

`/probe?days=7` 실측:
```
load_fcst[2026-08-01:컬럼없음(<?xml version="1.0"?>), ...]   ← 모든 상품·모든 날짜 동일
```
ERCOT MIS는 **같은 리포트를 CSV와 XML 두 개의 별도 문서로 발행**한다.
`pick_doc()`이 발행시각만 보고 가장 최신 것을 골랐는데, XML이 CSV보다 몇 초 늦게 올라오는 경우가 많아
**항상 XML 문서를 집고 있었다.** 그걸 `pd.read_csv`로 읽으니 `<?xml version="1.0"?>` 한 줄이
컬럼명이 되어버렸고, 날짜 컬럼을 못 찾아 그 발행일이 통째로 스킵됐다.

v10까지는 이 상황이 `날짜파싱실패`로 뭉뚱그려져 보였고, v11에서 컬럼명을 찍기 시작하면서 정체가 드러났다.
**백필이 한 번도 안 됐던 이유가 이것 하나다.** (7일 보관 한도 문제가 아니었음)

v12 수정
- `doc_fmt(d)` — 문서의 FriendlyName/FileName/Extension으로 csv/xml 판별
- `pick_doc()` — **CSV 문서만** 후보로 삼고, 그 안에서 06~11시 창의 최신본 선택
- `mis_read_csv()` — zip 안에 .csv가 없으면 `zip에CSV없음:...` 으로 명시적 실패
- `/peek` 에 `candidates_that_day` 추가 — 그날 발행된 문서들을 fmt와 함께 나열

### 배포 후 확인
```
https://power-model.onrender.com/probe?days=7
```
`recoverable_days` 에 날짜가 채워지면 정상. 그 날짜들이 워크플로 1회 실행으로 백필된다.

## 13. actuals 중복·구멍 수정 (v13)

증상: 같은 `dt_local`이 값만 다르게 두 번(`00:00 rt 40.64` / `00:00 rt 38.65`), 그리고 시간대가 뭉텅뭉텅 비어 있음.

원인 세 가지
1. **XML 문서 혼입** — 정산 리포트도 CSV/XML 양쪽으로 발행되는데 `[:max_docs]`가 목록 앞 6개를
   순서 보장 없이 잘라 썼다. XML을 CSV로 읽다 실패하면 `except: pass`로 조용히 버려져 날짜가 듬성해졌다.
2. **미완결 시간 기록** — RT는 15분 4구간 평균인데, 진행 중인 시간(2~3구간)의 평균을 그대로 적었다.
   다음 실행에서 4구간이 다 차면 값이 달라진 채로 **또 append** → 중복 행.
3. **일 단위 스킵** — `have_actual_days`가 "그날 한 행이라도 있으면 그 날 전체를 스킵"이라
   부분만 기록된 날의 나머지 23시간이 **영구히** 안 채워졌다. 구멍이 고착된 진짜 이유.

수정
- `_recent_csv_docs()` — CSV 문서만, 발행일 내림차순 정렬 후 최근 20개 (기존 6개 무정렬)
- RT는 `n >= 4`(4구간 완결)인 시간만 채택. 같은 시각의 부분판/완결판이 함께 오면 **완결판 우선**
- `have_actual_hours`(정확한 dt_local 목록)로 **시간 단위** 스킵. 워크플로 v10이 이 값을 보냄
- DAM/RTM 파서도 `_c` / `_parse_dates` / `_hours` 기반으로 교체

### 기존 오염된 행 정리
이미 들어간 중복·부분값은 자동으로 고쳐지지 않는다(그 시각은 이제 `have_actual_hours`에 포함되어 스킵됨).
`actuals` 탭을 **헤더만 남기고 비운 뒤** 워크플로를 1회 수동 실행하면 MIS 보관분 범위 내에서 깨끗하게 재구축된다.

## 14. 쿼터 초과 2차 — 읽기 노드가 아이템마다 실행 (워크플로 v11)

`Read headers`에서 쿼터가 났지만 범인은 그 노드가 아니다(호출 1회). 위쪽 읽기 노드들이었다.

**n8n 노드는 기본적으로 입력 아이템마다 한 번씩 실행된다.**
`Read state tab`이 720행 = 720 아이템을 뱉으면 → `Read predictions (days)`가 **720번** 실행 →
그 출력이 다시 `Read actuals (days)`로 들어가 **수백 번** 더. 분당 60회 한도를 즉시 초과하고,
소진된 상태에서 `Read headers`가 마지막에 걸려 그 노드 이름으로 에러가 표시된 것뿐이다.

**v11 해결**: Sheets 읽기를 전부 `values:batchGet` **1회**로 통합. 노드 3개(+헤더노드) → 1개.

읽는 range 9개 (순서 고정)
```
0 state!A:Z          3 predictions!1:1     6 actuals!1:1
1 predictions!A:A    4 model_detail!1:1    7 daily_summary!1:1
2 actuals!A:A        5 state!1:1           8 runlog!1:1
```
`Pack payload`가 0~2를, `Build appends`가 3~8(헤더)을 사용한다.
`valueRenderOption=UNFORMATTED_VALUE`로 숫자를 문자열이 아닌 숫자로 받는다.

| | v8 | v9 | **v11** |
|---|---|---|---|
| 노드 수 | 24 | 15 | **12** |
| Sheets API 호출/실행 | 수백~수천 | 읽기 4 + 쓰기 ≤6 | **읽기 1 + 쓰기 ≤6 = 7** |

부수 효과
- 모든 중간 노드에 `executeOnce: true` → 아이템 수와 무관하게 1회 실행 보장
  (`Append rows (batch)`만 예외 — 탭당 1회 실행해야 하므로)
- `Pack payload`가 state 192행 미만이면 명확한 메시지로 즉시 실패 (예전엔 Render에서 400)
- 재임포트 시 두 HTTP 노드(`Read sheets (1 call)`, `Append rows (batch)`)에
  **Predefined Credential Type → Google Sheets OAuth2 API** 연결 필요

## 15. 배포 버전 확인
```
GET https://power-model.onrender.com/health
→ {"ok":true,"version":"v13","trained_through":"2026-07-25 18:00:00", ...}
```
`version`이 기대값과 다르면 Render가 아직 이전 커밋을 돌리고 있는 것 —
Render 대시보드에서 Manual Deploy → Deploy latest commit.

## 16. actuals가 여전히 안 채워질 때 — /settle (v14)

`fetch_settlements`는 실패를 전부 `except: pass`로 삼켜서 왜 비었는지 알 수 없었다. v14에서 진단을 노출.

```
https://power-model.onrender.com/settle
```
시트 상태와 무관하게 MIS만 조회해서, 정산값이 만들어지는 **전 과정을 단계별로** 보여준다.

`diag.notes`를 위에서부터 읽으면 어디서 끊겼는지 나온다:
```
dam:CSV문서 12개
rtm:CSV문서 12개
DAM시간 288 / RT시간 288 (구간수분포 {2:2, 4:286}) → 완결(n>=4) 286
DA∩RT 교집합 286시간
범위 2026-07-21 00:00:00~2026-08-01 21:00:00
시트에 이미 있어 스킵 0 → 신규 286행
```

단계별 진단
| 어디서 0이 되나 | 원인 |
|---|---|
| `CSV문서 0개` | 문서 목록에 CSV가 없음 — 리포트 ID(12331/12301) 확인 필요 |
| `diag.dam[]`에 `실패(...)` 만 있음 | 파싱 실패. 괄호 안 예외 메시지가 원인 (컬럼명 변경 등) |
| `완결(n>=4) 0` | RT 구간수 분포 확인. `{1: ...}`이면 그 리포트가 이미 시간단위 → `?min_intervals=1` |
| `DA∩RT 교집합 0` | DAM/RTM의 시각 정렬이 어긋남 (HourEnding 해석 문제) |
| `신규 0행` | 값은 다 있는데 전부 시트에 이미 존재 — 정상 |

`/score` 응답의 `meta.settlement_diag`에도 같은 내용이 들어가므로, 워크플로 실행 후 runlog로도 추적 가능.

파라미터: `/settle?max_docs=40` (더 과거까지), `/settle?min_intervals=1` (완결 필터 해제)

## 17. 시트 실측 점검 결과 (2026-08-02) 및 워크플로 v12

Google Drive로 시트를 직접 읽어 확인한 것들.

### 확인된 정상
- `state` — runlog 기준 2026-07-06~08-03 (07-26 제외) 보유. **백필 성공**
- `runlog` — 01:02:56 실행에서 8일 전부 `mis+state`로 처리, skipped 없음
- `predictions` / `model_detail` — 내용 정상

### 발견된 문제 3가지 → v12에서 수정

**(1) predictions 중복 append.**
`2026-08-01`이 120행(=24×5), `2026-07-25`가 48행(=24×2). 중복 타임스탬프 48개.
원인: `/score`는 내일치를 **항상** 다시 내보낸다(`need_pred or day == tomorrow`).
하루에 워크플로를 5번 수동 실행하면 같은 날 판정이 5벌 쌓인다.
→ v12: `Build appends`가 `predictions!A:A` / `model_detail!A:A`의 기존 dt_local과 대조해
**이미 있는 시각은 제외**. 몇 번을 재실행해도 중복이 생기지 않는다.

**(2) daily_summary가 안 채워짐 — v9에서 내가 넣은 회귀.**
시트 헤더는 `run_date, target_day, n_buy_da, n_neutral, n_buy_rt, max_p_spike,
model_3class_summary, spike_model_summary, final_recommendation` 9개인데,
v9 `Build appends`는 `run_at, n_da, n_rt, claude_summary` 등 다른 키를 내보냈다.
헤더 기준 매핑이라 **거의 전 컬럼이 빈칸**이 된다.
→ v12: 시트의 기존 9컬럼 스키마 그대로 출력. Claude 노드도 JSON 3부 구조
(`model_3class_summary` / `spike_model_summary` / `final_recommendation`)로 응답하도록 복구.
코드펜스가 붙어 와도 벗겨내고, 파싱 실패 시 원문을 첫 칸에 넣는 폴백 포함.

**(3) runlog 컬럼 부족.**
정산 진단(v14의 `settlement_diag`)을 담을 자리가 없었다.
→ runlog 헤더 끝에 **`n_settlements`, `settlement_notes` 2개 추가 필요**:
```
run_at  target_day  todo_days  processed  skipped  backfilled  state_rows_returned
state_days_in_sheet  model_detail_rows  n_settlements  settlement_notes
```
이제 매 실행마다 정산이 몇 행 나왔는지, 안 나왔으면 왜인지가 runlog에 남는다.
(`processed`/`skipped` 구분자도 `|` → `;` 로 변경 — `|`는 CSV/마크다운 내보내기에서 열이 밀린다)

### 기존 중복 정리
`predictions` 탭에서 `dt_local` 기준 중복 제거(가장 최근 `generated_at`만 남김) 후 v12 사용.
데이터 → 데이터 정리 → 중복 항목 삭제 에서 `dt_local` 열만 선택하면 되지만,
그러면 **먼저 나온 행**이 남으므로 `generated_at` 내림차순 정렬 후 실행할 것.

## 18. actuals가 항상 비던 진짜 원인 — RTM은 15분마다 1행 (v17)

`/settle` 실측:
```
dam:CSV문서 20개 → 각 "행24"        ← DAM은 하루 1문서 × 24행 (정상)
rtm:CSV문서 20개 → 각 "행1"         ← RTM은 15분마다 1문서 × 1행
RT시간 5 (구간수분포 {2: 5}) → 완결(n>=4) 0
DA∩RT 교집합 0시간 → 신규 0행
```

두 리포트의 **발행 단위가 완전히 다릅니다.**
DAM(12331)은 하루 한 번 24행짜리 문서를 내지만,
RTM(12301)은 **15분마다 그 구간 1개만** 담은 문서를 냅니다(하루 96개).

v16까지의 코드는 문서마다 `groupby(hour).mean()`을 한 뒤 **시간 기준으로 중복 제거**했습니다.
문서 1개 = 구간 1개이므로 시간당 4개 문서가 서로를 덮어써서 3개가 버려졌고,
`n`은 영원히 1~2에 머물러 `n>=4` 완결 조건을 **구조적으로 만족할 수 없었습니다.**
v13에서 제가 넣은 "4구간 완결" 필터가 정당했음에도 결과가 항상 0이던 이유입니다.

### v17 수정
- `parse_rtm_intervals()` — 문서를 `(시각, 구간번호, 가격)` 구간 단위로 반환
- 모든 문서를 **구간 단위로 concat** → `(ts, iv)` 기준 중복 제거 → 그 다음에 시간별 집계
- 필요한 발행일(기본 최근 2일)의 문서만 선별 수신, 상한 220개
- **이미 시트에 있는 시간대의 문서는 받지 않음** — 발행시각으로 딜리버리 시간을 추정해 제외
  → 첫 실행은 ~192개, 이후 매일 실행은 ~96개만 받습니다

### 예상 동작
```
rtm:전체문서 N개 / 발행일 M일 → 대상 [...] 후보 192개 중 기수집분 제외하고 192개 수신
RT 구간행 192 → 시간 48 (구간수분포 {4: 48}) → 완결(n>=4) 48
DA∩RT 교집합 48시간 → 신규 48행
```
`구간수분포`가 `{4: ...}` 로 나오면 정상입니다. `{1: ...}` 나 `{2: ...}` 면 여전히 구간이 덜 모인 것.

### 파라미터
`/settle?rtm_days=3` (더 과거까지 · 문서 수 비례 증가) ·
`/settle?max_rtm_docs=400` (상한 상향) · `/settle?min_intervals=1` (완결 필터 해제, 진단용)

**주의**: RTM 문서를 수백 개 받으므로 첫 실행은 2~4분 걸릴 수 있습니다.
n8n `POST /score` 타임아웃은 600초로 잡혀 있어 여유가 있습니다.
