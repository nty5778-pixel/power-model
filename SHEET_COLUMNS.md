# ERCOT DART Tracker — 시트 컬럼 레퍼런스

모든 시각은 **미국 중부시간(CPT)**, 시간은 **시작시각 기준**(`00:00` = 00~01시).
DART = **DA_SPP − RT_SPP** (양수 = RT가 저렴, 음수 = DA가 저렴).

---

## predictions — 매일 쓰는 판정 결과

| 컬럼 | 의미 | 읽는 법 |
|---|---|---|
| `dt_local` | 딜리버리 시각 | |
| `p_da_cheap` | P(DART < −$1) = DA가 유의미하게 쌀 확률 | 5개 모델 평균 |
| `p_neutral` | P(\|DART\| ≤ $1) | |
| `p_rt_cheap` | P(DART > +$1) = RT가 유의미하게 쌀 확률 | |
| `p_spike` | P(DART < −$10) — 별도 스파이크 모델 | **보정 확률 아님.** 상대 랭킹으로만 사용 |
| `e_dart` | 기대 스프레드 = Σ pₖ·μₖ, μ=[−12.83, 0.03, +10.80] | 실제 판정을 만드는 값 |
| `decision` | `e_dart < −0.5` → BUY_DA / `> +0.5` → BUY_RT / else NEUTRAL_5050 | |
| `generated_at` | 산출 시각 | 10:00 CPT(DAM 마감) 이전이어야 유효 |

`p_rt_cheap`이 높아도 `e_dart`가 밴드 안이면 중립입니다. 판정은 확률이 아니라 **기대값**이 만듭니다.

---

## model_detail — 앙상블 평균 전, 개별 모델 출력

`predictions`는 5개 모델을 평균한 뒤의 결과만 담습니다. 이 탭은 평균 내기 **전** 원본입니다.
시드 7 / 17 / 42 / 101 / 202 — 학습 데이터와 피처는 동일하고 부트스트랩·피처샘플링 난수만 다릅니다.

| 컬럼 | 의미 |
|---|---|
| `ens_e_dart`, `ens_decision`, `ens_p_spike` | 실제 운용에 쓰이는 앙상블 값 (predictions와 동일) |
| `m3_<시드>_p_da` | 그 모델의 P(DA 저렴) |
| `m3_<시드>_p_rt` | 그 모델의 P(RT 저렴). `p_neutral = 1 − p_da − p_rt` |
| `m3_<시드>_e_dart` | 그 모델 **단독**의 기대 스프레드 |
| `m3_<시드>_decision` | 그 모델이 **혼자 판정했다면** 나왔을 결론 |
| `ms_<시드>_p_spike` | 스파이크 모델 5개 각각의 P(DART < −$10) |
| `agree_n` | 5개 중 앙상블 판정과 같은 결론을 낸 개수 (5 = 만장일치) |
| `e_dart_std` | 시드별 `e_dart`의 표준편차 — **판정 신뢰도의 핵심 지표** |
| `e_dart_min` / `e_dart_max` | 시드별 기대값의 최소·최대 |
| `p_spike_std` | 스파이크 모델 간 분산 |

### 실제 행으로 읽어보기 (2026-07-27 00:00)

```
ens_e_dart 4.721  ens_decision BUY_RT  agree_n 5
시드별 e_dart: 5.793 / 2.765 / 4.574 / 5.540 / 4.932   →  std 1.069, 범위 2.765~5.793
```

판정은 만장일치 BUY_RT입니다. 다만 **`e_dart_std` 1.069는 결정 밴드 $0.5의 두 배**입니다.
방향은 다섯 모델이 모두 같지만 크기 추정은 시드마다 $2.77~$5.79로 두 배 넘게 갈렸다는 뜻입니다.
방향 판정으로는 신뢰할 만하고(모두 밴드 위), 이 시간의 `e_dart`를 크기로 쓰는 건 조심해야 합니다.

`p_spike`는 5개 모두 0.003~0.007로 사실상 0 — 하방 리스크 없는 평범한 RT 우위 시간입니다.

### 운영 규칙 후보

- `agree_n ≤ 3` → 모델끼리 방향이 갈린 시간. 실무에서 중립 취급 검토
- `e_dart_std > $0.5`인데 `agree_n = 5` → 방향은 믿고 크기는 믿지 않기
- `e_dart_min`과 `e_dart_max`의 **부호가 다르면** 앙상블 판정이 시드 운에 좌우된 것
- 주간 리뷰에서 시드별 `decision` vs 실제 `dart`를 집계하면 어느 시드가 지속 열위인지 보입니다
  → 다음 재학습 때 교체 후보

---

## state — 그때 무엇을 예보했나 (입력 기록 보관소)

모델 피처를 만드는 원재료입니다. **전부 예보값**이고, D-1 아침 MIS 발행분에서 복원합니다.

| 컬럼 | 의미 | 출처 |
|---|---|---|
| `lf_sys` | 시스템 전체 부하예보 (MW) | ERCOT 7일 부하예보 |
| `stwpf` | 풍력 발전예보 P50 (MW) | ERCOT 풍력예보 |
| `wgrpp` | 풍력 발전예보 P80 (MW) | 〃 |
| `stppf` | 태양광 발전예보 P50 (MW) | ERCOT 태양광예보 |
| `temp_fcst` | 휴스턴 기온예보 (°C) | Open-Meteo |
| `outage_total` | 4개 존 합산 정지용량 (MW) | ERCOT 자원정지 |
| `outage_houston` | 휴스턴 존 정지용량 | 〃 |
| `outage_irr` | 4개 존 합산 간헐성자원 정지 | 〃 |
| `temp_act`, `wind`, `solar` | **비워둠 (정상)** | 실적값이라 D-1에 존재하지 않음 |

`wgrpp − stwpf`가 `wind_unc` 피처 — ERCOT 자체 풍력 불확실도입니다.
실적 기반 피처(`wind_err_lag48` 등)는 이 탭이 아니라 **매 실행 시 EIA·Open-Meteo API에서 직접** 가져와
48시간 전 예보(state에 기록된 값)와 빼서 만듭니다. 실적을 시트에 이중 저장하지 않는 이유입니다.

---

## actuals — 사후 정산값

| 컬럼 | 의미 |
|---|---|
| `dt_local` | 딜리버리 시각 |
| `da_spp` | LZ_HOUSTON DAM 정산가 ($/MWh) |
| `rt_spp` | LZ_HOUSTON RTM 정산가 — 15분 4구간 평균 |
| `dart` | `da_spp − rt_spp` |

RT는 **4구간이 모두 확정된 시간만** 기록됩니다(v13). 진행 중인 시간의 부분평균은 나중에 값이 바뀌므로 제외합니다.

---

## runlog — 매 실행 진단

| 컬럼 | 의미 |
|---|---|
| `run_at` | 실행 시각 |
| `target_day` | 내일 (주 판정 대상일) |
| `todo_days` | 이번 실행이 처리하려 한 날짜 전체 |
| `processed` | `날짜:출처(+state)` — `mis` = MIS에서 복원, `state` = 시트 값 재사용 |
| `skipped` | 실패한 날짜와 **사유 문자열**. 비어 있어야 정상 |
| `backfilled` | 내일치 외에 추가로 채운 날짜 |
| `state_rows_returned` | 이번에 state에 새로 쓴 행 수 |
| `state_days_in_sheet` | 실행 시점에 state가 보유 중이던 날짜 |
| `model_detail_rows` | model_detail에 쓴 행 수 |

`skipped` 사유 예: `wind[2026-08-01:없음,...]` = 그 발행일에 문서 없음 /
`날짜파싱실패(col=...,예='...')` = 파일은 받았는데 파싱 실패 / `행0(문서범위 A~B)` = 문서에 그 날짜가 없음.

---

## daily_summary — Claude 일일 코멘트

`run_at`, `target_day`, `n_da` / `n_neutral` / `n_rt` (판정 분포), `max_p_spike`,
`low_agree_hours` (agree_n ≤ 3 시간 수), `avg_e_dart_std`, `backfilled`, `claude_summary`.

## analysis — Claude 주간 리포트 (월 09:00)

`week_ending`, `n_hours_7d`, `hit_7d`, `save_vs_half_7d`, `save_vs_rt_7d`,
`hit_cum`, `save_vs_rt_cum`, `worst_hour_7d`, `claude_analysis`.

---

## 성과 지표 정의

- **적중**: BUY_DA → 실제 DART < 0 / BUY_RT → DART > 0 / NEUTRAL → \|DART\| ≤ $1
- **vs 항상RT**: 진짜 벤치마크. RT 100% 구매 대비 절감 ($/MWh). 기대치 **+$0.3~0.4**
- **vs 반반**: DA·RT 50:50 대비 절감. 기대치 +$1.9~2.0 (달성이 쉬워 참고용)

적중률과 PnL은 갈라질 수 있습니다. 작은 방향 판정을 접고 큰 건에 몰아주면 적중률은 내려가고
절감은 올라갑니다. **판단은 vs 항상RT 기준으로** 하시고, 이 값이 7일 기준 2주 연속 음수면
모델 재검증 트리거입니다.
