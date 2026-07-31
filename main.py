"""
ERCOT DART 3-class procurement scorer — Render web service. (v6: 백필 지원)

POST /score
  body: {
    "state": [{dt_local, lf_sys, stwpf, wgrpp, stppf, temp_fcst,
               outage_total, outage_houston, outage_irr}, ...],
    "weather":      {"time":[...], "temperature_2m":[...]},   # Open-Meteo forecast(past_days 포함)
    "weather_prev": {"time":[...], "temperature_2m_previous_day1":[...]},  # 선택: 과거일 D-1 예보
    "have_pred_days":   ["2026-07-30", ...],   # 선택: predictions 탭에 이미 있는 날짜
    "have_actual_days": ["2026-07-29", ...],   # 선택: actuals 탭에 이미 있는 날짜
    "max_backfill_days": 7                     # 선택 (기본 7, MIS 보관한도)
  }
returns: {predictions:[...], new_state:[...], settlements:[...], meta:{...}}

동작:
  - 내일치를 항상 처리하고, 최근 max_backfill_days 이내에서
    state가 비었거나 predictions가 없는 날짜를 자동으로 찾아 함께 백필한다.
  - state가 이미 있는 날은 MIS를 다시 받지 않고 state 값으로 피처를 만든다(판정만 백필).
  - state가 없는 날은 그날의 D-1 아침 발행분을 MIS 아카이브에서 찾아 복원한다(7일 보관 한도).

GET /health -> ok
"""
import os, io, json, zipfile, datetime as dt
import requests
import numpy as np
import pandas as pd
import lightgbm as lgb
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
M3 = [lgb.Booster(model_file=os.path.join(HERE, "models", f"m3_{s}.txt")) for s in CFG["seeds"]]
MS = [lgb.Booster(model_file=os.path.join(HERE, "models", f"ms_{s}.txt")) for s in CFG["seeds"]]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MIS_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={rid}"
MIS_DL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={docid}"
RID = {"load_fcst": 12312, "wind": 13028, "solar": 13483, "outage": 13103,
       "dam_spp_daily": 12331, "rtm_spp_daily": 12301}

STATE_COLS = ["lf_sys", "stwpf", "wgrpp", "stppf", "temp_fcst",
              "outage_total", "outage_houston", "outage_irr"]

app = FastAPI()


class ScoreReq(BaseModel):
    state: list
    weather: dict | None = None
    weather_prev: dict | None = None
    have_pred_days: list | None = None
    have_actual_days: list | None = None
    max_backfill_days: int = 7


# ---------------- MIS helpers ----------------
def mis_doc_list(rid, cache):
    if rid in cache:
        return cache[rid]
    j = requests.get(MIS_LIST.format(rid=rid), headers=UA, timeout=60).json()
    docs = [d["Document"] for d in j["ListDocsByRptTypeRes"]["DocumentList"]]
    cache[rid] = docs
    return docs


def pick_doc(docs, pub_day, lo=6, hi=11):
    """pub_day에 발행된 문서 중 lo~hi시 창의 마지막(가장 최신) 것."""
    same = [d for d in docs if str(d.get("PublishDate", ""))[:10] == pub_day.isoformat()]
    if not same:
        return None
    win = [d for d in same if lo <= int(str(d["PublishDate"])[11:13]) <= hi]
    pool = win or same
    return sorted(pool, key=lambda d: d["PublishDate"])[-1]


def mis_read_csv(docid):
    r = requests.get(MIS_DL.format(docid=docid), headers=UA, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))


def hours_of(day):
    return pd.date_range(pd.Timestamp(day), periods=24, freq="h")



def _safe(o):
    """reindex 전에 중복 라벨 제거(방어적)."""
    try:
        if o.index.has_duplicates:
            o = o[~o.index.duplicated(keep="last")]
    except Exception:
        pass
    return o

def dedup(s):
    return s[~s.index.duplicated(keep="last")].sort_index()


def fetch_day_inputs(target, cache):
    """딜리버리일=target 의 발행 예측치를 MIS에서 복원. 실패 시 None."""
    pub = target - dt.timedelta(days=1)
    out = {}

    d = pick_doc(mis_doc_list(RID["load_fcst"], cache), pub)
    if d is None:
        return None
    df = mis_read_csv(d["DocID"])
    df = df[df["DSTFlag"] == "N"]
    sel = df[pd.to_datetime(df["DeliveryDate"], format="%m/%d/%Y") == pd.Timestamp(target)]
    if len(sel) == 0:
        return None
    he = sel["HourEnding"].astype(str).str.split(":").str[0].astype(int)
    out["lf_sys"] = dedup(pd.Series(sel["SystemTotal"].values,
                                    index=pd.Timestamp(target) + pd.to_timedelta(he - 1, unit="h")))

    d = pick_doc(mis_doc_list(RID["wind"], cache), pub)
    if d is None:
        return None
    w = mis_read_csv(d["DocID"])
    w = w[w["DSTFlag"] == "N"]
    sel = w[pd.to_datetime(w["DELIVERY_DATE"], format="%m/%d/%Y") == pd.Timestamp(target)]
    if len(sel) == 0:
        return None
    i2 = pd.Timestamp(target) + pd.to_timedelta(sel["HOUR_ENDING"].astype(int) - 1, unit="h")
    out["stwpf"] = dedup(pd.Series(sel["STWPF_SYSTEM_WIDE"].values, index=i2))
    out["wgrpp"] = dedup(pd.Series(sel["WGRPP_SYSTEM_WIDE"].values, index=i2))

    d = pick_doc(mis_doc_list(RID["solar"], cache), pub)
    if d is None:
        return None
    s = mis_read_csv(d["DocID"])
    s = s[s["DSTFlag"] == "N"]
    sel = s[pd.to_datetime(s["DELIVERY_DATE"], format="%m/%d/%Y") == pd.Timestamp(target)]
    if len(sel) == 0:
        return None
    i3 = pd.Timestamp(target) + pd.to_timedelta(sel["HOUR_ENDING"].astype(int) - 1, unit="h")
    out["stppf"] = dedup(pd.Series(sel["STPPF_SYSTEM_WIDE"].values, index=i3))

    d = pick_doc(mis_doc_list(RID["outage"], cache), pub)
    if d is None:
        return None
    o = mis_read_csv(d["DocID"])
    sel = o[pd.to_datetime(o["Date"], format="%m/%d/%Y") == pd.Timestamp(target)]
    if len(sel) == 0:
        return None
    i4 = pd.Timestamp(target) + pd.to_timedelta(sel["HourEnding"].astype(int) - 1, unit="h")
    tot = sel[["TotalResourceMWZoneSouth", "TotalResourceMWZoneNorth",
               "TotalResourceMWZoneWest", "TotalResourceMWZoneHouston"]].sum(axis=1)
    irr = sel[["TotalIRRMWZoneSouth", "TotalIRRMWZoneNorth",
               "TotalIRRMWZoneWest", "TotalIRRMWZoneHouston"]].sum(axis=1)
    out["outage_total"] = dedup(pd.Series(tot.values, index=i4))
    out["outage_houston"] = dedup(pd.Series(sel["TotalResourceMWZoneHouston"].values, index=i4))
    out["outage_irr"] = dedup(pd.Series(irr.values, index=i4))
    return out


def inp_from_state(hist, target):
    """state에 이미 있는 날은 MIS 재조회 없이 그 값으로 입력 구성."""
    hrs = hours_of(target)
    out = {}
    for c in STATE_COLS:
        if c not in hist.columns:
            return None
        s = hist[c].pipe(_safe).reindex(hrs)
        out[c] = s
    if out["lf_sys"].isna().all():
        return None
    return out


def fetch_eia_actuals(days_back=10):
    key = os.environ.get("EIA_API_KEY")
    if not key:
        return None
    end = dt.datetime.now(CT)
    start = end - dt.timedelta(days=days_back)
    u = ("https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
         f"?api_key={key}&frequency=hourly&data[0]=value&facets[respondent][]=ERCO"
         f"&facets[fueltype][]=WND&facets[fueltype][]=SUN"
         f"&start={start:%Y-%m-%dT%H}&end={end:%Y-%m-%dT%H}&length=5000")
    try:
        j = requests.get(u, timeout=60).json()
    except Exception:
        return None
    rows = j.get("response", {}).get("data", [])
    df = pd.DataFrame(rows)
    if len(df) == 0:
        return None
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["dt"] = (pd.to_datetime(df["period"], utc=True).dt.tz_convert(CT).dt.tz_localize(None)
                - pd.Timedelta(hours=1))  # hour-ending 보정
    piv = df.pivot_table(index="dt", columns="fueltype", values="value", aggfunc="first")
    piv = piv.rename(columns={"WND": "wind", "SUN": "solar"})
    return piv[~piv.index.duplicated(keep="last")]


def fetch_settlements(cache, skip_days, max_docs=6):
    """최근 확정일들의 LZ_HOUSTON DA/RT. skip_days(이미 시트에 있는 날짜)는 제외."""
    def parse_dam(df):
        pc = "SettlementPoint" if "SettlementPoint" in df.columns else "Settlement Point"
        df = df[df[pc] == "LZ_HOUSTON"]
        vc = "SettlementPointPrice" if "SettlementPointPrice" in df.columns else "Settlement Point Price"
        dc = "DeliveryDate" if "DeliveryDate" in df.columns else "Delivery Date"
        hc = "HourEnding" if "HourEnding" in df.columns else "Hour Ending"
        he = df[hc].astype(str).str.split(":").str[0].astype(int)
        return pd.Series(pd.to_numeric(df[vc], errors="coerce").values,
                         index=pd.to_datetime(df[dc]) + pd.to_timedelta(he - 1, unit="h"))

    def parse_rtm(df):
        pcol = [c for c in df.columns if c.replace(" ", "") in ("SettlementPointName", "SettlementPoint")]
        if pcol:
            df = df[df[pcol[0]] == "LZ_HOUSTON"]
        dc = "DeliveryDate" if "DeliveryDate" in df.columns else "Delivery Date"
        hc = [c for c in df.columns if c.replace(" ", "") == "DeliveryHour"][0]
        vc = [c for c in df.columns if "Price" in c][0]
        idx = pd.to_datetime(df[dc]) + pd.to_timedelta(df[hc].astype(int) - 1, unit="h")
        return pd.Series(pd.to_numeric(df[vc], errors="coerce").values, index=idx).groupby(level=0).mean()

    das, rts = [], []
    try:
        for d in mis_doc_list(RID["dam_spp_daily"], cache)[:max_docs]:
            try:
                das.append(parse_dam(mis_read_csv(d["DocID"])))
            except Exception:
                pass
    except Exception:
        pass
    try:
        for d in mis_doc_list(RID["rtm_spp_daily"], cache)[:max_docs]:
            try:
                rts.append(parse_rtm(mis_read_csv(d["DocID"])))
            except Exception:
                pass
    except Exception:
        pass
    if not das or not rts:
        return []
    da = dedup(pd.concat(das))
    rt = dedup(pd.concat(rts))
    both = pd.concat([da.rename("da"), rt.rename("rt")], axis=1).dropna()
    out = []
    for t, row in both.iterrows():
        if str(t)[:10] in skip_days:
            continue
        out.append({"dt_local": str(t), "da_spp": round(float(row["da"]), 2),
                    "rt_spp": round(float(row["rt"]), 2),
                    "dart": round(float(row["da"] - row["rt"]), 2)})
    return out


# ---------------- feature builder ----------------
def build_features(target, inp, hist, eia, temps_now, temp_fcst_series):
    hrs = hours_of(target)
    f = pd.DataFrame(index=hrs)
    f["hour"] = f.index.hour
    f["dow"] = f.index.dayofweek
    f["month"] = f.index.month
    f["is_weekend"] = (f["dow"] >= 5).astype(int)
    f["rtcb"] = 1

    lf = inp["lf_sys"].pipe(_safe).reindex(hrs)
    f["op_df_level"] = lf.values
    lf_all = dedup(pd.concat([hist["lf_sys"].dropna(), lf.dropna()]))
    f["op_df_ramp1"] = lf_all.diff(1).pipe(_safe).reindex(hrs).values
    f["op_df_dpeak"] = float(np.nanmax(lf.values)) if lf.notna().any() else np.nan

    win_end = pd.Timestamp(target) - pd.Timedelta(days=2)

    def same_hour_mean(series, h, days=7):
        s = series.dropna()
        s = s[(s.index >= win_end - pd.Timedelta(days=days - 1)) &
              (s.index < win_end + pd.Timedelta(days=1))]
        s = s[s.index.hour == h]
        return float(s.mean()) if len(s) else np.nan

    f["op_df_anom"] = [f["op_df_level"].iloc[i] - same_hour_mean(hist["lf_sys"], h)
                       for i, h in enumerate(f["hour"])]

    tf = temp_fcst_series.pipe(_safe).reindex(hrs)
    f["temp_fcst"] = tf.values
    f["wx_cdd"] = (f["temp_fcst"] - 21).clip(lower=0)
    f["wx_hdd"] = (10 - f["temp_fcst"]).clip(lower=0)
    f["wx_dmax"] = float(np.nanmax(tf.values)) if tf.notna().any() else np.nan
    past_temps = temps_now[temps_now.index < pd.Timestamp(target)]
    f["wx_anom"] = [f["temp_fcst"].iloc[i] - same_hour_mean(past_temps, h)
                    for i, h in enumerate(f["hour"])]

    d2 = hours_of((pd.Timestamp(target) - pd.Timedelta(days=2)).date())
    f["wx_err_lag48"] = (temps_now.pipe(_safe).reindex(d2).values - hist["temp_fcst"].pipe(_safe).reindex(d2).values)
    if eia is not None:
        f["wind_err_lag48"] = eia["wind"].pipe(_safe).reindex(d2).values - hist["stwpf"].pipe(_safe).reindex(d2).values
        f["solar_err_lag48"] = eia["solar"].pipe(_safe).reindex(d2).values - hist["stppf"].pipe(_safe).reindex(d2).values
    else:
        f["wind_err_lag48"] = np.nan
        f["solar_err_lag48"] = np.nan

    stw = inp["stwpf"].pipe(_safe).reindex(hrs)
    stp = inp["stppf"].pipe(_safe).reindex(hrs)
    f["wind_unc"] = (stw - inp["wgrpp"].pipe(_safe).reindex(hrs)).values
    f["outage_total"] = inp["outage_total"].pipe(_safe).reindex(hrs).values
    f["outage_houston"] = inp["outage_houston"].pipe(_safe).reindex(hrs).values
    f["outage_irr"] = inp["outage_irr"].pipe(_safe).reindex(hrs).values
    f["op_scarcity"] = f["op_df_level"] - stw.values - stp.values + f["outage_total"]

    o_hist = hist["outage_total"].dropna()
    o_hist = o_hist[o_hist.index < pd.Timestamp(target) - pd.Timedelta(days=1)].tail(14 * 24)
    f["outage_anom"] = f["outage_total"] - (float(o_hist.mean()) if len(o_hist) else np.nan)
    return f


def predict_day(f):
    F3 = f[CFG["features_3class"]].astype(float)
    FS = f[CFG["features_spike"]].astype(float)
    F3 = F3.fillna(F3.median())
    FS = FS.fillna(FS.median())
    p3 = np.mean([b.predict(F3.values) for b in M3], axis=0)
    ps = np.mean([b.predict(FS.values) for b in MS], axis=0)
    mu = CFG["mu"]
    ED = p3[:, 0] * mu[0] + p3[:, 1] * mu[1] + p3[:, 2] * mu[2]
    band = CFG["band"]
    dec = np.where(ED < -band, "BUY_DA", np.where(ED > band, "BUY_RT", "NEUTRAL_5050"))
    if CFG.get("spike_threshold"):
        dec = np.where(ps > CFG["spike_threshold"], "BUY_DA", dec)
    return p3, ps, ED, dec


def _r(s, t):
    try:
        v = s.pipe(_safe).reindex([t]).iloc[0]
        return None if pd.isna(v) else round(float(v), 2)
    except Exception:
        return None


# ---------------- endpoints ----------------
@app.get("/health")
def health():
    return {"ok": True, "trained_through": CFG["trained_through"]}


@app.post("/score")
def score(req: ScoreReq):
    import traceback
    try:
        return _score(req)
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc().splitlines()
        mine = [l.strip() for l in tb if "main.py" in l]
        raise HTTPException(500, f"{type(e).__name__}: {e} | at {mine[-2:] if mine else tb[-3:]}")


def _score(req: ScoreReq):
    now = dt.datetime.now(CT)
    tomorrow = (now + dt.timedelta(days=1)).date()

    # ---- state 로드 ----
    st = pd.DataFrame(req.state)
    if len(st) < 24 * 8:
        raise HTTPException(400, f"state too short: {len(st)} rows (need >= 192)")
    st["dt_local"] = pd.to_datetime(st["dt_local"])
    st = st.set_index("dt_local").sort_index()
    st = st[~st.index.duplicated(keep="last")]
    for c in st.columns:
        st[c] = pd.to_numeric(st[c], errors="coerce")
    for c in STATE_COLS:
        if c not in st.columns:
            st[c] = np.nan
    hist = st.copy()

    # 날짜별 state 보유 여부 (24시간 중 20시간 이상이면 보유로 간주)
    day_counts = hist["lf_sys"].dropna().groupby(hist["lf_sys"].dropna().index.date).size()
    state_days = set(d for d, n in day_counts.items() if n >= 20)

    have_pred = set(str(x)[:10] for x in (req.have_pred_days or []))
    have_act = set(str(x)[:10] for x in (req.have_actual_days or []))

    # ---- 날씨 시리즈 ----
    if not (req.weather and req.weather.get("time")):
        raise HTTPException(400, "weather 필드가 필요합니다 (n8n Fetch Open-Meteo 노드)")
    temps_now = dedup(pd.Series(req.weather["temperature_2m"],
                                index=pd.to_datetime(req.weather["time"])))
    temps_prev = None
    if req.weather_prev and req.weather_prev.get("time"):
        vk = [k for k in req.weather_prev if k.startswith("temperature_2m")]
        if vk:
            temps_prev = dedup(pd.Series(req.weather_prev[vk[0]],
                                         index=pd.to_datetime(req.weather_prev["time"])))

    # ---- 처리 대상 날짜 결정 ----
    nback = max(1, min(int(req.max_backfill_days or 7), 10))
    candidates = [tomorrow - dt.timedelta(days=k) for k in range(nback, -1, -1)]
    min_state_day = min(state_days) if state_days else None
    todo = []
    for d in candidates:
        if min_state_day and d < min_state_day:
            continue
        need_state = d not in state_days
        need_pred = (d.isoformat() not in have_pred) if req.have_pred_days is not None else (d == tomorrow)
        if need_state or need_pred:
            todo.append(d)
    if tomorrow not in todo:
        todo.append(tomorrow)
    todo = sorted(set(todo))

    eia = fetch_eia_actuals()
    cache = {}
    preds_out, state_out, processed, skipped = [], [], [], []

    for day in todo:
        try:
            from_state = day in state_days
            inp = inp_from_state(hist, day) if from_state else fetch_day_inputs(day, cache)
            if inp is None:
                skipped.append({"day": day.isoformat(),
                                "reason": "MIS 발행분 없음(보관기한 초과 또는 미발행)"})
                continue

            hrs = hours_of(day)
            tfs = None
            if day < tomorrow and temps_prev is not None:
                cand = temps_prev.pipe(_safe).reindex(hrs)
                if cand.notna().sum() >= 12:
                    tfs = cand
            if tfs is None:
                tfs = (inp["temp_fcst"].pipe(_safe).reindex(hrs) if from_state and "temp_fcst" in inp
                       else temps_now.pipe(_safe).reindex(hrs))
                if tfs.isna().all():
                    tfs = temps_now.pipe(_safe).reindex(hrs)
            inp["temp_fcst"] = tfs

            f = build_features(day, inp, hist, eia, temps_now, tfs)
            p3, ps, ED, dec = predict_day(f)

            need_pred = (day.isoformat() not in have_pred) if req.have_pred_days is not None else True
            if need_pred or day == tomorrow:
                for i in range(24):
                    preds_out.append({
                        "dt_local": str(hrs[i]),
                        "p_da_cheap": round(float(p3[i, 0]), 4),
                        "p_neutral": round(float(p3[i, 1]), 4),
                        "p_rt_cheap": round(float(p3[i, 2]), 4),
                        "p_spike": round(float(ps[i]), 4),
                        "e_dart": round(float(ED[i]), 3),
                        "decision": str(dec[i]),
                    })

            if not from_state:
                for i in range(24):
                    state_out.append({"dt_local": str(hrs[i]),
                                      **{c: _r(inp[c], hrs[i]) for c in STATE_COLS}})
                add = pd.DataFrame({c: inp[c].pipe(_safe).reindex(hrs) for c in STATE_COLS}, index=hrs)
                hist = pd.concat([hist, add])
                hist = hist[~hist.index.duplicated(keep="last")].sort_index()
                state_days.add(day)

            processed.append({"day": day.isoformat(),
                              "source": "state" if from_state else "mis",
                              "state_written": not from_state})
        except Exception as e:
            skipped.append({"day": day.isoformat(), "reason": f"{type(e).__name__}: {e}"})

    settlements = fetch_settlements(cache, have_act)

    return {
        "predictions": preds_out,
        "new_state": state_out,
        "settlements": settlements,
        "meta": {
            "target_day": str(tomorrow),
            "generated_at": str(now),
            "processed_days": processed,
            "skipped_days": skipped,
            "backfilled_days": [p["day"] for p in processed if p["day"] != str(tomorrow)],
            "config": {k: CFG[k] for k in ("band", "spike_threshold", "trained_through")},
        },
    }