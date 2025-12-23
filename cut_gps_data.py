'''
截取 GPS 数据的部分片段
'''
import csv
import os
from datetime import datetime, timezone, timedelta

# ================= 配置区 =================

input_csv = "./data/灵敢足迹（2025.12.22）.csv"
output_dir = "./output"
output_csv = os.path.join(output_dir, "cut.csv")

MODE = "time"   # "line" 或 "time"

# ---------- 行号模式 ----------
LINE_START = 2      # 起始行（包含表头算第1行）
LINE_END = 10       # 结束行

# ---------- 时间模式 ----------
START_TIME = "2025-03-17 00:00:00"
END_TIME   = "2025-03-20 23:59:00"

TIMEZONE_OFFSET = 2     # UTC+2
GEOTIME_COLUMN = "geoTime"

# ==========================================


def time_to_geotime(time_str):
    time_format = "%Y-%m-%d %H:%M:%S"
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
    dt = datetime.strptime(time_str, time_format)
    dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


os.makedirs(output_dir, exist_ok=True)

with open(input_csv, newline='', encoding="utf-8") as f:
    reader = list(csv.reader(f))

header = reader[0]
data = reader[1:]

# ================= 按行截取 =================
if MODE == "line":
    selected = reader[LINE_START - 1 : LINE_END]

# ================= 按时间截取 =================
elif MODE == "time":
    start_ts = time_to_geotime(START_TIME)
    end_ts = time_to_geotime(END_TIME)

    geo_idx = header.index(GEOTIME_COLUMN)

    selected_data = []
    for row in data:
        try:
            ts = int(row[geo_idx])
            if start_ts <= ts <= end_ts:
                selected_data.append(row)
        except ValueError:
            continue

    selected = [header] + selected_data

else:
    raise ValueError("MODE 只能是 'line' 或 'time'")

# ================= 输出 =================
with open(output_csv, "w", newline='', encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerows(selected)

print("输出完成:", output_csv)