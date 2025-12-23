'''
修复工程主程序
自动修复 GPS 轨迹中的坐标系跳变问题
'''
import pandas as pd
import math
import numpy as np

# ---------------- 配置区域 ----------------
INPUT_FILE = './output/cut.csv'    # 乱序文件
OUTPUT_FILE = './output/gps_data_perfect.csv' # 修复后的文件
JUMP_DETECT_THRESHOLD = 50.0       # 下限：超过此值判定为异常跳变
SMOOTH_THRESHOLD = 800.0            # 上限：修复后小于此值才视为物理合理
MIN_IMPROVEMENT = 4.0              # 最小收益：修复必须改善至少 x m 才值得做
AMBIGUOUS_THRESHOLD = 120.0         # 不确定区间：|improvement| < 此值时启用第三点
LOOKAHEAD_GAIN = 20.0              # 前瞻收益阈值：防止微小差异触发修复
# ----------------------------------------

# --- 1. 基础算法：GCJ-02 转 WGS-84 (逆向纠偏) ---
# 这是把“跑偏”的高德坐标拉回 GPS 坐标的公式
def gcj02_to_wgs84(lng, lat):
    x_pi = 3.14159265358979324 * 3000.0 / 180.0
    pi = 3.1415926535897932384626
    a = 6378245.0
    ee = 0.00669342162296594323
    
    def out_of_china(lat, lon):
        if lon < 72.004 or lon > 137.8347: return True
        if lat < 0.8293 or lat > 55.8271: return True
        return False
    
    def transform_lat(x, y):
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * pi) + 320 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
        return ret
    
    def transform_lon(x, y):
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
        return ret
        
#    if out_of_china(lat, lng): return lng, lat
    dlat = transform_lat(lng - 105.0, lat - 35.0)
    dlng = transform_lon(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * pi)
    mglat = lat + dlat
    mglng = lng + dlng
    return lng * 2 - mglng, lat * 2 - mglat

# --- 2. 距离计算工具 (Haversine) ---
def get_distance(lon1, lat1, lon2, lat2):
    R = 6371000 # 地球半径
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# --- 3. 核心修复逻辑 ---
def auto_repair_trajectory(file_path, output_path):
    print("读取数据...")
    df = pd.read_csv(file_path)
    
    # 1. 预处理：按时间排序 + 暴力去重
    df = df.sort_values(by='geoTime')
    df = df.drop_duplicates(subset=['geoTime'], keep='first').reset_index(drop=True)
    
    print(f"有效数据点: {len(df)}")
    
    # 结果容器
    fixed_lons = []
    fixed_lats = []
    notes = []
    debug_logs = []
    
    # 初始化第一个点
    last_valid_lon = df.loc[0, 'longitude']
    last_valid_lat = df.loc[0, 'latitude']
    
    fixed_lons.append(last_valid_lon)
    fixed_lats.append(last_valid_lat)
    notes.append("Start")
    
    print("正在进行平滑修复...")
    
    for i in range(1, len(df)):
        # 当前点的"原始坐标"
        curr_raw_lon = df.loc[i, 'longitude']
        curr_raw_lat = df.loc[i, 'latitude']
        
        # 当前点的"备选坐标" (假设它是GCJ，转回WGS试试)
        curr_fix_lon, curr_fix_lat = gcj02_to_wgs84(curr_raw_lon, curr_raw_lat)
        
        # 计算两个假设与"上一个点"的距离
        dist_if_original = get_distance(last_valid_lon, last_valid_lat, curr_raw_lon, curr_raw_lat)
        dist_if_fixed = get_distance(last_valid_lon, last_valid_lat, curr_fix_lon, curr_fix_lat)
        
        improvement = dist_if_original - dist_if_fixed

        cond_jump = dist_if_original > JUMP_DETECT_THRESHOLD
        cond_smooth = dist_if_fixed < SMOOTH_THRESHOLD
        cond_improve = improvement > MIN_IMPROVEMENT

        # ---------- 一阶强判 ----------
        lookahead_used = False
        lookahead_decision = None
        cost_raw = None
        cost_fix = None
        
        # 条件 1：明显异常跳变且修复后合理 → 直接修
        if cond_jump and improvement >= MIN_IMPROVEMENT and cond_smooth:
            final_lon, final_lat = curr_fix_lon, curr_fix_lat
            note = "REPAIRED (GCJ->WGS)"
            decision = "REPAIRED"
        
        # 条件 2：模糊区 → 启用第三点裁决（在否决之前！）
        elif abs(improvement) < AMBIGUOUS_THRESHOLD and i + 1 < len(df):
            lookahead_used = True
            
            # 获取下一个点（原始坐标，不对它做修复）
            next_raw_lon = df.loc[i + 1, 'longitude']
            next_raw_lat = df.loc[i + 1, 'latitude']
            
            # 路径 A：不修 i
            cost_raw = (
                get_distance(last_valid_lon, last_valid_lat, curr_raw_lon, curr_raw_lat)
                + get_distance(curr_raw_lon, curr_raw_lat, next_raw_lon, next_raw_lat)
            )
            
            # 路径 B：修 i
            cost_fix = (
                get_distance(last_valid_lon, last_valid_lat, curr_fix_lon, curr_fix_lat)
                + get_distance(curr_fix_lon, curr_fix_lat, next_raw_lon, next_raw_lat)
            )
            
            if cost_fix + LOOKAHEAD_GAIN < cost_raw:
                final_lon, final_lat = curr_fix_lon, curr_fix_lat
                note = "REPAIRED (via LOOKAHEAD)"
                decision = "LOOKAHEAD_FIX"
                lookahead_decision = "FIX"
            else:
                final_lon, final_lat = curr_raw_lon, curr_raw_lat
                note = "Original (via LOOKAHEAD)"
                decision = "LOOKAHEAD_RAW"
                lookahead_decision = "RAW"
        
        # 条件 3：明显不该修 → 直接 ORIGINAL
        elif improvement <= -MIN_IMPROVEMENT:
            final_lon, final_lat = curr_raw_lon, curr_raw_lat
            note = "Original"
            decision = "ORIGINAL"
        
        # 条件 4：兜底
        else:
            final_lon, final_lat = curr_raw_lon, curr_raw_lat
            note = "Reset/Unsure"
            decision = "RESET"

        # --- 记录 debug 日志 ---
        debug_logs.append({
            "index": i,
            "geoTime": df.loc[i, "geoTime"],
            "last_lon": last_valid_lon,
            "last_lat": last_valid_lat,
            "raw_lon": curr_raw_lon,
            "raw_lat": curr_raw_lat,
            "fix_lon": curr_fix_lon,
            "fix_lat": curr_fix_lat,
            "dist_if_original": dist_if_original,
            "dist_if_fixed": dist_if_fixed,
            "improvement": improvement,
            "cond_jump": cond_jump,
            "cond_smooth": cond_smooth,
            "cond_improve": cond_improve,
            "lookahead_used": lookahead_used,
            "lookahead_decision": lookahead_decision,
            "cost_raw": cost_raw,
            "cost_fix": cost_fix,
            "decision": decision,
            "note": note
        })
        
        # 更新结果
        fixed_lons.append(final_lon)
        fixed_lats.append(final_lat)
        notes.append(note)
        
        # 更新"上一个有效点"
        last_valid_lon = final_lon
        last_valid_lat = final_lat
        
    df['clean_longitude'] = fixed_lons
    df['clean_latitude'] = fixed_lats
    df['repair_note'] = notes
    
    print("-" * 30)
    print("修复统计:")
    print(df['repair_note'].value_counts())
    print("-" * 30)
    
    df.to_csv(output_path, index=False)
    print(f"完成! 请使用 clean_longitude 和 clean_latitude 绘图。")
    
    # 导出 debug 日志
    debug_df = pd.DataFrame(debug_logs)
    debug_df.to_csv("./output/debug_decisions.csv", index=False)
    print("Debug 日志已保存: ./output/debug_decisions.csv")


auto_repair_trajectory(INPUT_FILE, OUTPUT_FILE)