#!/usr/bin/env python3
"""Fetch electricity price data from InfluxDB → price_data.json → git push to price-monitor GH Pages.

Fetches ALL provinces with spot price data.
"""

import json, os, subprocess, sys
from datetime import datetime, timezone, timedelta
from influxdb_client import InfluxDBClient

INFLUX_URL = "http://192.168.1.89:8086"
TOKEN="ToFZ-ewNYaj_m09su2dFb2EKJAAOX3k5nK0Wy00fS46gcItE7R24EBJb_UhKYmXCCkUoVZ1XQKX9H4e_pDcooA=="
ORG = "shenshu"
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
OUTPUT = os.path.join(SCRIPT_DIR, "price_data.json")

BJT = timezone(timedelta(hours=8))

# All provinces with spot price data
SPOT_PROVINCES = [
    "上海", "云南", "吉林", "四川", "宁夏", "安徽", "山东", "山西",
    "广东", "广西", "江苏", "江西", "河南", "浙江", "海南", "湖北",
    "湖南", "福建", "贵州", "辽宁", "陕西", "青海", "黑龙江"
]

# All provinces with agent price data (subset that also have buckets)
AGENT_PROVINCES = SPOT_PROVINCES  # query will handle empty results gracefully

def to_bjt_str(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M")

def to_bjt_date(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJT).strftime("%Y-%m-%d")

def fetch_spot(client, province, days=30):
    r = {"province": province, "type": "spot", "day_ahead": [], "real_time": []}
    qapi = client.query_api()
    for pt in ("day_ahead", "real_time"):
        flux = f'''from(bucket: "electricity_price")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r["_measurement"] == "clear_price" and r["region"] == "{province}" and r["priceType"] == "{pt}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])'''
        try:
            tables = qapi.query(flux)
            records = []
            for table in tables:
                for rec in table.records:
                    records.append({"t": to_bjt_str(rec["_time"]), "p": round(float(rec["price"]), 1)})
            if pt == "day_ahead": r["day_ahead"] = records
            else: r["real_time"] = records
        except Exception as e:
            print(f"  ⚠️  spot_{province}/{pt}: {e}")
    return r

def fetch_agent(client, province, months=24):
    flux = f'''from(bucket: "electricity_price")
  |> range(start: -{months}mo)
  |> filter(fn: (r) => r["_measurement"] == "agent_price" and r["region"] == "{province}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])'''
    r = {"province": province, "type": "agent", "records": []}
    try:
        tables = client.query_api().query(flux)
        seen = set()  # 按 YYYY-MM 去重（同一个月只保留第一条）
        for table in tables:
            for rec in table.records:
                bjt = rec["_time"].astimezone(timezone(timedelta(hours=8)))
                mk = bjt.strftime("%Y-%m")  # 只取年月，去除日+时区偏移歧义
                if mk in seen: continue
                seen.add(mk)
                entry = {"t": mk + "-01"}  # 统一显示为当月1日
                for f in ("purchasingPrice", "lineLossCost", "purchasingSystemOperatingCost", "purchasingSum"):
                    try: entry[f] = round(float(rec[f]), 4)
                    except: pass
                r["records"].append(entry)
        r["records"].sort(key=lambda x: x["t"])
    except Exception as e:
        print(f"  ⚠️  agent_{province}: {e}")
    return r

def fetch_mid_long(client, province, months=24):
    flux = f'''from(bucket: "electricity_price")
  |> range(start: -{months}mo)
  |> filter(fn: (r) => r["_measurement"] == "mid_long_term_price" and r["region"] == "{province}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])'''
    r = {"province": province, "type": "mid_long", "records": []}
    try:
        tables = client.query_api().query(flux)
        seen = set()
        for table in tables:
            for rec in table.records:
                bjt = rec["_time"].astimezone(timezone(timedelta(hours=8)))
                mk = bjt.strftime("%Y-%m")
                if mk in seen: continue
                seen.add(mk)
                r["records"].append({"t": mk + "-01", "p": round(float(rec["price"]), 1)})
        r["records"].sort(key=lambda x: x["t"])
    except Exception as e:
        print(f"  ⚠️  mid_long_{province}: {e}")
    return r

def main():
    client = InfluxDBClient(url=INFLUX_URL, token=TOKEN, org=ORG, timeout=60_000)
    data = {"generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"), "spot": [], "agent": [], "mid_long": []}

    print(f"Fetching {len(SPOT_PROVINCES)} provinces...")
    for prov in SPOT_PROVINCES:
        print(f"  {prov}...", end=" ", flush=True)
        data["spot"].append(fetch_spot(client, prov))
        data["agent"].append(fetch_agent(client, prov))
        data["mid_long"].append(fetch_mid_long(client, prov))
        last = data["spot"][-1]
        print(f"DA={len(last['day_ahead'])} RT={len(last['real_time'])}")

    client.close()
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=None)
    size = os.path.getsize(OUTPUT)
    print(f"\n✅ {OUTPUT} ({size/1024:.1f} KB)")

    # Git push
    repo = SCRIPT_DIR
    try:
        subprocess.run(["git", "-C", repo, "add", "price_data.json", "index.html"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", f"price_data: update {data['generated_at']}"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "push"], check=True, capture_output=True)
        print("  ✅ git push done")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode()
        if "nothing to commit" in stderr:
            print("  ℹ️  No changes")
        else:
            print(f"  ⚠️  {stderr[:200]}")

if __name__ == "__main__":
    main()
