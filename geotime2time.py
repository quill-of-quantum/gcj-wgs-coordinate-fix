from datetime import datetime, timezone, timedelta

# ================= 只需要修改这里 =================
geo_time_input = 1650072179000

# 时区偏移量：你软件显示的是10点，比UTC(8点)快2小时，所以这里填 2
TIMEZONE_OFFSET = 2  
# ================================================

def geotime_to_custom_date(timestamp_ms, offset_hours):
    # 定义自定义时区
    custom_tz = timezone(timedelta(hours=offset_hours))
    
    try:
        timestamp_s = float(timestamp_ms) / 1000.0
        dt_obj = datetime.fromtimestamp(timestamp_s, custom_tz)
        return dt_obj.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] 
    except Exception as e:
        return f"转换出错: {e}"

# 运行转换
result = geotime_to_custom_date(geo_time_input, TIMEZONE_OFFSET)

print("-" * 30)
print(f"输入 geoTime: {geo_time_input}")
print(f"时区设置:     UTC+{TIMEZONE_OFFSET}")
print(f"转换结果:     {result}")
print("-" * 30)