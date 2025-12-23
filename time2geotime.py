from datetime import datetime, timezone, timedelta

# ================= 只需要修改这里 =================
target_time = "2022-04-16 03:22:00"

# 时区偏移量：为了和你软件保持一致，这里改为 2 (即 UTC+2)
TIMEZONE_OFFSET = 2
# ================================================

def time_to_geotime(time_str):
    # 1. 设置格式
    time_format = "%Y-%m-%d %H:%M:%S"
    
    # 2. 定义目标时区 (UTC+2)
    target_tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
    
    # 3. 解析时间字符串
    try:
        dt_obj = datetime.strptime(time_str, time_format)
        
        # 4. 强制指定这个时间是 "UTC+2" 的时间
        dt_obj = dt_obj.replace(tzinfo=target_tz)
        
        # 5. 转为 13位 时间戳
        timestamp = int(dt_obj.timestamp() * 1000)
        
        return timestamp
    except ValueError:
        return "格式错误，请检查是否为 '年-月-日 时:分:秒' 格式"

# 运行转换
result = time_to_geotime(target_time)

print("-" * 30)
print(f"输入时间: {target_time}")
print(f"时区设定: UTC+{TIMEZONE_OFFSET}")
print(f"转换结果: {result}")
print("-" * 30)