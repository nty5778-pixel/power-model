# 배포 체크리스트 (한 번에 끝내기용) — 2026-08-02

시트를 직접 읽어 코드와 대조한 결과입니다. **순서대로** 하시면 재작업 없습니다.

---

## STEP 1. 시트 손보기 (n8n·Render 건드리기 전)

### 1-1. `runlog` 헤더 끝에 4칸 추가
현재 9칸 → **13칸**. J1~M1에 붙여넣기:
```
n_settlements	settlement_notes	eia_ok	feature_health
```
`eia_ok` / `feature_health`는 2차 점검에서 추가된 항목입니다 (아래 STEP 2 참고).

### 1-2. `analysis` 탭 신규 생성 ← **없으면 주간 워크플로가 400으로 죽습니다**
현재 시트에 탭이 6개(predictions, state, actuals, daily_summary, model_detail, runlog)뿐입니다.
새 탭 `analysis` 를 만들고 1행에:
```
week_ending	n_hours_7d	hit_7d	save_vs_half_7d	save_vs_rt_7d	hit_cum	save_vs_rt_cum	worst_hour_7d	claude_analysis
```

### 1-3. `predictions` 중복 제거
`2026-08-01`이 120행(24×5), `2026-07-25`가 48행(24×2). 중복 타임스탬프 48개.
1. `generated_at` **내림차순** 정렬 (최신이 위로)
2. 데이터 → 데이터 정리 → 중복 항목 삭제 → **`dt_local` 열만** 체크
   (구글은 위쪽 행을 남기므로 정렬을 먼저 해야 최신 판정이 살아남습니다)

### 1-4. `actuals` — 비어 있는 상태 그대로 두기 (이미 초기화하셨습니다)

### 확인 완료된 것 (손댈 필요 없음)
- `model_detail` 헤더 35칸 — 코드 출력과 **완전 일치**
- `predictions` / `state` / `actuals` / `daily_summary` 헤더 — 모두 일치
- `state` — 07-06~08-03 백필 완료 (07-26 제외)

---

## STEP 2. Render 배포 (main.py v18)

```
https://power-model.onrender.com/health
→ {"ok":true,"version":"v18","eia_key_set":true,"n_models":10, ...}
```
`v18`이 아니면 Render 대시보드 → Manual Deploy → Deploy latest commit.

### v16 추가 — EIA 키가 죽으면 조용히 품질이 떨어진다
`EIA_API_KEY`가 없거나 EIA API가 실패하면 `eia = None`이 되고
`wind_err_lag48` / `solar_err_lag48` **2개 피처가 전 시간 NaN**이 됩니다.
LightGBM은 NaN을 그냥 처리하므로 **예측은 정상적으로 나오고 아무 에러도 없습니다.**
18개 중 2개가 죽은 채로 몇 주가 지나도 모를 수 있었습니다.

- `/health`에 `eia_key_set` 노출
- `meta.eia` = {ok, key_set, rows, last}
- `meta.feature_health` = 내일치 피처 중 **결측률 50% 초과** 항목과 누락 항목
- runlog의 `eia_ok` / `feature_health` 칸에 매 실행 기록

**`/health`에서 `eia_key_set: false`면 Render 환경변수부터 확인하세요.**

### v15에서 고친 것 — **재작업을 막는 핵심**
시트가 `dt_local`을 날짜값으로 저장해서 **`2026-07-25 0:00:00`** (0 패딩 없음)으로 돌려줍니다.
코드는 `00:00:00`을 쓰므로 문자열 비교가 **전부 불일치**합니다. 그 결과:
- 중복 방지 필터가 무력화 → predictions가 계속 중복 append
- `have_actual_hours` 스킵 실패 → actuals도 중복
- 주간 분석의 predictions↔actuals 조인이 **0건**이 되어 리포트가 항상 빈 값

`_norm_ts()`(Python)와 `norm()`(n8n JS)로 양쪽 다 정규화했습니다.
`0:00:00` / `00:00:00` / ISO / 구글 시리얼 숫자 모두 같은 값으로 맞춥니다.

---

## STEP 3. 워크플로 임포트 (일일 v14 / 주간 v2)

**둘 다 재임포트가 필요합니다.**

### 일일 (v14, 12노드)
### 주간 (v2, 6노드) — 기존 버전은 Merge combineAll이 predictions×actuals 카티전 곱을 만들고
Sheets 노드가 아이템마다 실행되어 쿼터를 터뜨립니다. batchGet 1회 구조로 교체했습니다.

### 임포트 후 크리덴셜 연결 (총 5곳)
| 워크플로 | 노드 | 크리덴셜 |
|---|---|---|
| 일일 | `Read sheets (1 call)` | Predefined → **Google Sheets OAuth2 API** |
| 일일 | `Append rows (batch)` | 〃 |
| 일일 | `Claude 일일 요약` | Header Auth (`x-api-key` = Anthropic 키) |
| 주간 | `Read sheets (1 call)` | Predefined → Google Sheets OAuth2 API |
| 주간 | `Append analysis` | 〃 |
| 주간 | `Claude 주간 분석` | Header Auth (`x-api-key`) |

Render URL은 하드코딩되어 있으므로 수정 불필요.

---

## STEP 4. 수동 실행 → 확인

실행 후 `runlog` 마지막 행에서 이 순서로 봅니다.

| 컬럼 | 정상값 | 이상하면 |
|---|---|---|
| `skipped` | 비어 있음 | 사유 문자열이 그대로 원인 |
| `model_detail_rows` | 24 (백필 시 더 큼) | 0이면 예측 자체가 없음 |
| `n_settlements` | 0보다 큼 | **0이면 `/settle` 확인** |
| `settlement_notes` | 단계별 로그 | 어느 줄에서 0이 되는지가 원인 |
| `eia_ok` | `ok(240행, ~...)` | `실패(key_set=false)`면 Render 환경변수 누락 |
| `feature_health` | `ok` | 결측 피처가 나열되면 그만큼 모델이 눈을 감고 있는 것 |

`predictions`는 **재실행해도 행 수가 늘지 않아야** 정상입니다(중복 필터 작동 확인).

### actuals 채우기 (v18)
RTM 8일치를 다 받으려면 **수동 실행 3~4회**가 필요합니다. 매 실행 `runlog`의
`settlement_notes` 끝에 `잔여 N개는 다음 실행에서` 가 사라질 때까지 반복하세요.
`n_settlements`가 0이 되면 완료입니다.

---

## actuals — 원인 확정 및 수정 (v17)

`/settle` 결과로 원인이 나왔습니다: RTM(12301)은 **15분마다 1행짜리 문서**라
시간 단위 중복제거가 4구간 중 3개를 버리고 있었습니다 (README §18).
v17 배포 후 아래로 확인하세요:
```
https://power-model.onrender.com/settle
```
`diag.notes`를 위에서부터 읽으면 끊긴 지점이 보입니다.

| 어디서 0 | 원인 | 조치 |
|---|---|---|
| `CSV문서 0개` | 리포트 ID(12331/12301) 문제 | ID 재확인 필요 |
| `실패(...)` 만 나열 | 파싱 실패 | 괄호 안 예외 메시지가 단서 |
| `완결(n>=4) 0` | **제가 넣은 4구간 필터가 과함** | `/settle?min_intervals=1` 로 즉시 확인 |
| `DA∩RT 교집합 0` | DAM/RTM 시각 정렬 어긋남 | HourEnding 해석 수정 |

`/settle` 결과만 주시면 그 자리에서 고칩니다. 추측으로 먼저 고치지 않겠습니다.

---

## 이번 점검에서 찾은 것 요약

| # | 문제 | 상태 |
|---|---|---|
| 1 | MIS가 XML 문서를 CSV로 읽힘 → 백필 전면 실패 | v12에서 수정, **해결 확인** |
| 2 | 시트 `0:00:00` vs 코드 `00:00:00` 불일치 | v15 + 워크플로 v13에서 수정 |
| 3 | predictions 중복 append (내일치 매번 재출력) | 워크플로 v13에서 수정 |
| 4 | daily_summary 스키마 불일치 (v9에서 내가 낸 회귀) | 워크플로 v12에서 복구 |
| 5 | 주간 워크플로 Merge 카티전 곱 + 아이템별 실행 | 주간 v2에서 재작성 |
| 6 | `analysis` 탭 부재 | **STEP 1-2에서 생성 필요** |
| 7 | runlog에 정산 진단 자리 없음 | **STEP 1-1에서 컬럼 추가 필요** |
| 8 | actuals 미채움 — RTM은 15분마다 1행인데 시간단위로 덮어써서 3구간 유실 | **v17 수정 → 실측 정상 확인** |
| 11 | RTM 백필이 한 번에 2.3일치만 회수 | v18에서 순차 수렴 (3~4회 실행이면 완료) |
| 9 | EIA 실패 시 피처 2개가 조용히 NaN | v16에서 노출 (runlog에 기록) |
| 10 | 피처 결측률 무관측 | v16에서 `feature_health` 추가 |
