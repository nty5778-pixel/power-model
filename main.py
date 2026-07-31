"""
ERCOT DART 3-class procurement scorer — Render web service.

POST /score
  body: {"state": [{dt_local, lf_sys, stwpf, wgrpp, stppf, temp_fcst, outage_total,
                    outage_houston, outage_irr}, ...]}   # 최근 16일+ 시간별 (시트 state 탭)
  env:  EIA_API_KEY (필수)
returns: {predictions: [...24rows], new_state: [...24rows], settlements: [...], meta: {}}

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

app = FastAPI()

class ScoreReq(BaseModel):
    state: list
    weather: dict | None = None   # {"time": [...], "temperature_2m": [...]} — n8n이 Open-Meteo에서 받아 전달

def mis_latest_doc(rid, before=None):
    j = requests.get(MIS_LIST.format(rid=rid), headers=UA, timeout=60).json()
    docs = [d["Document"] for d in j["ListDocsByRptTypeRes"]["DocumentList"]]
    if before:
        docs = [d for d in docs if d["PublishDate"] <= before]
    return docs[0]

def mis_read_csv(docid):
    r = requests.get(MIS_DL.format(docid=docid), headers=UA, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))

def hours_of(day):
    return pd.date_range(pd.Timestamp(day), periods=24, freq="h")

def fetch_target_day_inputs(target, weather=None):
    """MIS 최신 발행분에서 딜리버리일=target 행 추출."""
    out = {}
    # load forecast (weather zones)
    df = mis_read_csv(mis_latest_doc(RID["load_fcst"])["DocID"])
    df = df[df["DSTFlag"] == "N"]
    dd = pd.to_datetime(df["DeliveryDate"], format="%m/%d/%Y")
    sel = df[dd == pd.Timestamp(target)]
    he = sel["HourEnding"].astype(str).str.split(":").str[0].astype(int)
    out["lf_sys"] = pd.Series(sel["SystemTotal"].values, index=pd.Timestamp(target) + pd.to_timedelta(he - 1, unit="h"))
    # wind
    w = mis_read_csv(mis_latest_doc(RID["wind"])["DocID"])
    w = w[w["DSTFlag"] == "N"]
    dd = pd.to_datetime(w["DELIVERY_DATE"], format="%m/%d/%Y")
    sel = w[dd == pd.Timestamp(target)]
    i2 = pd.Timestamp(target) + pd.to_timedelta(sel["HOUR_ENDING"].astype(int) - 1, unit="h")
    out["stwpf"] = pd.Series(sel["STWPF_SYSTEM_WIDE"].values, index=i2)
    out["wgrpp"] = pd.Series(sel["WGRPP_SYSTEM_WIDE"].values, index=i2)
    # solar
    s = mis_read_csv(mis_latest_doc(RID["solar"])["DocID"])
    s = s[s["DSTFlag"] == "N"]
    dd = pd.to_datetime(s["DELIVERY_DATE"], format="%m/%d/%Y")
    sel = s[dd == pd.Timestamp(target)]
    i3 = pd.Timestamp(target) + pd.to_timedelta(sel["HOUR_ENDING"].astype(int) - 1, unit="h")
    out["stppf"] = pd.Series(sel["STPPF_SYSTEM_WIDE"].values, index=i3)
    # outage
    o = mis_read_csv(mis_latest_doc(RID["outage"])["DocID"])
    dd = pd.to_datetime(o["Date"], format="%m/%d/%Y")
    sel = o[dd == pd.Timestamp(target)]
    i4 = pd.Timestamp(target) + pd.to_timedelta(sel["HourEnding"].astype(int) - 1, unit="h")
    tot = sel[["TotalResourceMWZoneSouth", "TotalResourceMWZoneNorth", "TotalResourceMWZoneWest", "TotalResourceMWZoneHouston"]].sum(axis=1)
    irr = sel[["TotalIRRMWZoneSouth", "TotalIRRMWZoneNorth", "TotalIRRMWZoneWest", "TotalIRRMWZoneHouston"]].sum(axis=1)
    out["outage_total"] = pd.Series(tot.values, index=i4)
    out["outage_houston"] = pd.Series(sel["TotalResourceMWZoneHouston"].values, index=i4)
    out["outage_irr"] = pd.Series(irr.values, index=i4)
    # weather: 내일 예보 + 최근 실적(analysis)
    if weather and weather.get("time"):
        tt = pd.to_datetime(weather["time"])
        temps = pd.Series(weather["temperature_2m"], index=tt)
    else:
        import time as _time
        wm = None
        for attempt in range(3):
            wm = requests.get("https://api.open-meteo.com/v1/forecast", params=dict(
                latitude=29.76, longitude=-95.36, hourly="temperature_2m",
                past_days=10, forecast_days=3, timezone="America/Chicago"), timeout=60).json()
            if "hourly" in wm:
                break
            _time.sleep(3)
        if "hourly" not in wm:
            raise HTTPException(502, f"Open-Meteo unavailable from server: {str(wm)[:200]} — n8n에서 weather를 body로 전달하세요")
        tt = pd.to_datetime(wm["hourly"]["time"])
        temps = pd.Series(wm["hourly"]["temperature_2m"], index=tt)
    out["temp_fcst"] = temps.reindex(hours_of(target))
    out["temp_recent"] = temps  # past analysis + forecast mix; 과거 구간은 실적 근사
    return out

def fetch_eia_actuals(days_back=6):
    key = os.environ.get("EIA_API_KEY")
    if not key:
        return None
    end = dt.datetime.now(CT)
    start = end - dt.timedelta(days=days_back)
    u = ("https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
         f"?api_key={key}&frequency=hourly&data[0]=value&facets[respondent][]=ERCO"
         f"&facets[fueltype][]=WND&facets[fueltype][]=SUN&start={start:%Y-%m-%dT%H}&end={end:%Y-%m-%dT%H}&length=5000")
    j = requests.get(u, timeout=60).json()
    rows = j.get("response", {}).get("data", [])
    df = pd.DataFrame(rows)
    if len(df) == 0:
        return None
    df["dt"] = pd.to_datetime(df["period"], utc=True).dt.tz_convert(CT).dt.tz_localize(None) - pd.Timedelta(hours=1)  # hour-ending 보정
    piv = df.pivot_table(index="dt", columns="fueltype", values="value", aggfunc="first")
    return piv.rename(columns={"WND": "wind", "SUN": "solar"})

def fetch_settlements():
    """최근 완결일의 LZ_HOUSTON DA/RT (MIS 일일 리포트)."""
    try:
        dam = mis_read_csv(mis_latest_doc(RID["dam_spp_daily"])["DocID"])
        dam = dam[(dam["SettlementPoint"] == "LZ_HOUSTON") if "SettlementPoint" in dam.columns else (dam["Settlement Point"] == "LZ_HOUSTON")]
        spcol = "SettlementPointPrice" if "SettlementPointPrice" in dam.columns else "Settlement Point Price"
        dcol = "DeliveryDate" if "DeliveryDate" in dam.columns else "Delivery Date"
        hcol = "HourEnding" if "HourEnding" in dam.columns else "Hour Ending"
        he = dam[hcol].astype(str).str.split(":").str[0].astype(int)
        da = pd.Series(dam[spcol].values, index=pd.to_datetime(dam[dcol]) + pd.to_timedelta(he - 1, unit="h"))
    except Exception:
        return []
    try:
        # RTM 일일: 최근 파일 여러 개가 인터벌별일 수 있음 — 최신 1개만 시도
        rt_doc = mis_latest_doc(RID["rtm_spp_daily"])
        rt = mis_read_csv(rt_doc["DocID"])
        pcol = [c for c in rt.columns if "SettlementPointName" in c.replace(" ", "") or "Settlement Point Name" == c]
        rt = rt[rt[pcol[0]] == "LZ_HOUSTON"] if pcol else rt
        dcol = "DeliveryDate" if "DeliveryDate" in rt.columns else "Delivery Date"
        hcol = [c for c in rt.columns if "DeliveryHour" in c.replace(" ", "")][0]
        vcol = [c for c in rt.columns if "Price" in c][0]
        rt["dt"] = pd.to_datetime(rt[dcol]) + pd.to_timedelta(rt[hcol].astype(int) - 1, unit="h")
        rts = rt.groupby("dt")[vcol].mean()
    except Exception:
        rts = pd.Series(dtype=float)
    out = []
    both = pd.concat([da.rename("da"), rts.rename("rt")], axis=1).dropna()
    for t, row in both.iterrows():
        out.append({"dt_local": str(t), "da_spp": round(float(row["da"]), 2),
                    "rt_spp": round(float(row["rt"]), 2), "dart": round(float(row["da"] - row["rt"]), 2)})
    return out

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
        raise HTTPException(500, f"{type(e).__name__}: {e} | {traceback.format_exc().splitlines()[-3:]}")

def _score(req: ScoreReq):
    st = pd.DataFrame(req.state)
    if len(st) < 24 * 10:
        raise HTTPException(400, f"state too short: {len(st)} rows (need >= 240)")
    st["dt_local"] = pd.to_datetime(st["dt_local"])
    st = st.set_index("dt_local").sort_index()
    st = st[~st.index.duplicated(keep="last")]
    for c in st.columns:
        st[c] = pd.to_numeric(st[c], errors="coerce")

    now = dt.datetime.now(CT)
    target = (now + dt.timedelta(days=1)).date()
    hrs = hours_of(target)

    inp = fetch_target_day_inputs(target, req.weather)
    eia = fetch_eia_actuals()

    # ---- 상태 결합 (과거 = state, 오늘 타깃 = 신규 fetch) ----
    hist = st.copy()
    lf_all = pd.concat([hist["lf_sys"].dropna(), inp["lf_sys"]]).sort_index()
    tf_all = pd.concat([hist["temp_fcst"].dropna(), inp["temp_fcst"]]).sort_index()
    stw_all = pd.concat([hist["stwpf"].dropna(), inp["stwpf"]]).sort_index()
    stp_all = pd.concat([hist["stppf"].dropna(), inp["stppf"]]).sort_index()
    out_all = pd.concat([hist["outage_total"].dropna(), inp["outage_total"]]).sort_index()
    out_all = out_all[~out_all.index.duplicated(keep="last")]

    f = pd.DataFrame(index=hrs)
    f["hour"] = f.index.hour; f["dow"] = f.index.dayofweek; f["month"] = f.index.month
    f["is_weekend"] = (f["dow"] >= 5).astype(int); f["rtcb"] = 1
    f["op_df_level"] = inp["lf_sys"].reindex(hrs)
    f["op_df_ramp1"] = lf_all.diff(1).reindex(hrs)
    f["op_df_dpeak"] = float(inp["lf_sys"].max())
    # 7d same-hour mean (D-8..D-2)
    win = pd.Timestamp(target) - pd.Timedelta(days=2)
    def same_hour_mean(series, h):
        s = series[(series.index >= win - pd.Timedelta(days=6)) & (series.index < win + pd.Timedelta(days=1))]
        s = s[s.index.hour == h]
        return float(s.mean()) if len(s) else np.nan
    f["op_df_anom"] = [f["op_df_level"].iloc[i] - same_hour_mean(hist["lf_sys"].dropna(), h) for i, h in enumerate(f["hour"])]
    f["temp_fcst"] = inp["temp_fcst"].values
    f["wx_cdd"] = (f["temp_fcst"] - 21).clip(lower=0)
    f["wx_hdd"] = (10 - f["temp_fcst"]).clip(lower=0)
    f["wx_dmax"] = float(np.nanmax(inp["temp_fcst"].values))
    temp_recent_past = inp["temp_recent"][inp["temp_recent"].index < pd.Timestamp(now.date())]
    f["wx_anom"] = [f["temp_fcst"].iloc[i] - same_hour_mean(temp_recent_past, h) for i, h in enumerate(f["hour"])]
    # wx_err_lag48: 실적(analysis) - 당시 발행 예보(state)
    d2 = pd.Timestamp(target) - pd.Timedelta(days=2)
    err48 = (inp["temp_recent"].reindex(hours_of(d2.date())).values
             - hist["temp_fcst"].reindex(hours_of(d2.date())).values)
    f["wx_err_lag48"] = err48
    # 재생 예측오차 lag48
    if eia is not None:
        we = eia["wind"].reindex(hours_of(d2.date())).values - hist["stwpf"].reindex(hours_of(d2.date())).values
        se = eia["solar"].reindex(hours_of(d2.date())).values - hist["stppf"].reindex(hours_of(d2.date())).values
    else:
        we = np.full(24, np.nan); se = np.full(24, np.nan)
    f["wind_err_lag48"] = we
    f["solar_err_lag48"] = se
    f["wind_unc"] = (inp["stwpf"] - inp["wgrpp"]).reindex(hrs).values
    # G5
    f["outage_total"] = inp["outage_total"].reindex(hrs).values
    f["outage_houston"] = inp["outage_houston"].reindex(hrs).values
    f["outage_irr"] = inp["outage_irr"].reindex(hrs).values
    f["op_scarcity"] = f["op_df_level"] - inp["stwpf"].reindex(hrs).values - inp["stppf"].reindex(hrs).values + f["outage_total"]
    o14 = out_all[out_all.index < pd.Timestamp(target) - pd.Timedelta(days=1)]
    f["outage_anom"] = f["outage_total"] - float(o14.tail(14 * 24).mean())

    # 결측 대체: 열 중위수 (러프하지만 안전)
    F3 = f[CFG["features_3class"]].astype(float)
    FS = f[CFG["features_spike"]].astype(float)
    F3 = F3.fillna(F3.median()); FS = FS.fillna(FS.median())

    p3 = np.mean([b.predict(F3.values) for b in M3], axis=0)
    ps = np.mean([b.predict(FS.values) for b in MS], axis=0)
    mu = CFG["mu"]
    ED = p3[:, 0] * mu[0] + p3[:, 1] * mu[1] + p3[:, 2] * mu[2]
    band = CFG["band"]
    dec = np.where(ED < -band, "BUY_DA", np.where(ED > band, "BUY_RT", "NEUTRAL_5050"))
    if CFG.get("spike_threshold"):
        dec = np.where(ps > CFG["spike_threshold"], "BUY_DA", dec)

    preds = [{"dt_local": str(hrs[i]), "p_da_cheap": round(float(p3[i, 0]), 4),
              "p_neutral": round(float(p3[i, 1]), 4), "p_rt_cheap": round(float(p3[i, 2]), 4),
              "p_spike": round(float(ps[i]), 4), "e_dart": round(float(ED[i]), 3),
              "decision": str(dec[i])} for i in range(24)]
    new_state = [{"dt_local": str(hrs[i]),
                  "lf_sys": _r(inp["lf_sys"], hrs[i]), "stwpf": _r(inp["stwpf"], hrs[i]),
                  "wgrpp": _r(inp["wgrpp"], hrs[i]), "stppf": _r(inp["stppf"], hrs[i]),
                  "temp_fcst": _r(inp["temp_fcst"], hrs[i]), "outage_total": _r(inp["outage_total"], hrs[i]),
                  "outage_houston": _r(inp["outage_houston"], hrs[i]), "outage_irr": _r(inp["outage_irr"], hrs[i])}
                 for i in range(24)]
    return {"predictions": preds, "new_state": new_state, "settlements": fetch_settlements(),
            "meta": {"target_day": str(target), "generated_at": str(now), "config": {k: CFG[k] for k in ("band", "spike_threshold", "trained_through")}}}

def _r(s, t):
    try:
        v = s.reindex([t]).iloc[0]
        return None if pd.isna(v) else round(float(v), 2)
    except Exception:
        return None