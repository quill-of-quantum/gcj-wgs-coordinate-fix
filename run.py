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
SHARP_TURN_DEG = 60.0              # 锐角阈值：小于此角度视为异常转向
SHARP_GAIN_MULTIPLIER = 50        # 锐角时的修复门槛倍数
# ----------------------------------------

# --- 1. 基础算法：GCJ-02 转 WGS-84 (逆向纠偏) ---
# 这是把"跑偏"的高德坐标拉回 GPS 坐标的公式
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

# --- 2.5. 角度计算工具 (方向连续性) ---
def turning_angle(p1, p2, p3):
    """
    计算在 p2 点的"转向角"（单位：度）
    p1, p2, p3: 元组 (lon, lat)
    
    返回值定义：
    - 180°：直线（无转向）
    - 90°：直角转弯
    - 0°：极度锐角 / 回头弯
    
    注意：考虑经度尺度随纬度缩放（cos(lat)）
    """
    # 中间点的纬度，用于修正经度差
    lat_mid = p2[1]
    cos_lat = math.cos(math.radians(lat_mid))
    
    # 修正后的向量（经度差乘以 cos(lat_mid)）
    v1 = np.array([
        (p2[0] - p1[0]) * cos_lat,
        p2[1] - p1[1]
    ])
    v2 = np.array([
        (p3[0] - p2[0]) * cos_lat,
        p3[1] - p2[1]
    ])
    
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    if norm1 == 0 or norm2 == 0:
        return 180.0  # 静止点，视为直线（不惩罚）
    
    cos_theta = np.dot(v1, v2) / (norm1 * norm2)
    cos_theta = np.clip(cos_theta, -1.0, 1.0)
    
    # 向量夹角（数学定义：0° 同向，180° 反向）
    angle_math = math.degrees(math.acos(cos_theta))
    
    # 转向角（业务定义：180° 直线，0° 极度锐角）
    turning_deg = 180.0 - angle_math
    
    return turning_deg

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
    prev_valid_lon = None  # 上上个输出点
    prev_valid_lat = None
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

        # ---------- 角度检查（前向 + 后向）----------
        sharp_turn = False
        angle_prev_raw = None
        angle_prev_fix = None
        angle_next_raw = None
        angle_next_fix = None
        required_improvement = MIN_IMPROVEMENT
        
        # 前向角度：a-b-c（角在 b，即 last_valid）
        if prev_valid_lon is not None:
            angle_prev_raw = turning_angle(
                (prev_valid_lon, prev_valid_lat),
                (last_valid_lon, last_valid_lat),
                (curr_raw_lon, curr_raw_lat)
            )
            
            angle_prev_fix = turning_angle(
                (prev_valid_lon, prev_valid_lat),
                (last_valid_lon, last_valid_lat),
                (curr_fix_lon, curr_fix_lat)
            )
        
        # 后向角度：c-d-e（角在 d，即 i+1）
        if i + 2 < len(df):
            next_raw_lon = df.loc[i + 1, 'longitude']
            next_raw_lat = df.loc[i + 1, 'latitude']
            next_next_raw_lon = df.loc[i + 2, 'longitude']
            next_next_raw_lat = df.loc[i + 2, 'latitude']
            
            angle_next_raw = turning_angle(
                (curr_raw_lon, curr_raw_lat),
                (next_raw_lon, next_raw_lat),
                (next_next_raw_lon, next_next_raw_lat)
            )
            
            angle_next_fix = turning_angle(
                (curr_fix_lon, curr_fix_lat),
                (next_raw_lon, next_raw_lat),
                (next_next_raw_lon, next_next_raw_lat)
            )
        
        # 判定是否存在锐角或显著变差
        angle_margin = 20.0  # 允许的角度变化容差
        
        if angle_prev_fix is not None and angle_prev_fix < SHARP_TURN_DEG:
            sharp_turn = True
        if angle_next_fix is not None and angle_next_fix < SHARP_TURN_DEG:
            sharp_turn = True
        
        # 修复导致角度显著变差
        if angle_prev_fix is not None and angle_prev_fix + angle_margin < angle_prev_raw:
            sharp_turn = True
        if angle_next_fix is not None and angle_next_fix + angle_margin < angle_next_raw:
            sharp_turn = True
        
        if sharp_turn:
            required_improvement = MIN_IMPROVEMENT * SHARP_GAIN_MULTIPLIER

        # ---------- 一阶强判 ----------
        lookahead_used = False
        lookahead_decision = None
        cost_raw = None
        cost_fix = None
        
        # 条件 1：明显异常跳变且修复后合理 → 直接修
        if cond_jump and cond_smooth:
            if improvement >= required_improvement:
                final_lon, final_lat = curr_fix_lon, curr_fix_lat
                note = "REPAIRED (GCJ->WGS)"
                decision = "REPAIRED"
            else:
                final_lon, final_lat = curr_raw_lon, curr_raw_lat
                note = "Original (SharpTurnBlocked)" if sharp_turn else "Original (InsufficientImprovement)"
                decision = "BLOCKED_BY_ANGLE" if sharp_turn else "BLOCKED_BY_IMPROVEMENT"
        
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
            
            # lookahead 也需考虑锐角惩罚
            lookahead_threshold = LOOKAHEAD_GAIN
            if sharp_turn:
                lookahead_threshold *= SHARP_GAIN_MULTIPLIER
            
            if cost_fix + lookahead_threshold < cost_raw:
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
            "prev_lon": prev_valid_lon,
            "prev_lat": prev_valid_lat,
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
            "angle_prev_raw": angle_prev_raw,
            "angle_prev_fix": angle_prev_fix,
            "angle_next_raw": angle_next_raw,
            "angle_next_fix": angle_next_fix,
            "sharp_turn": sharp_turn,
            "required_improvement": required_improvement,
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
        
        # 更新"上上个点"和"上一个有效点"
        prev_valid_lon = last_valid_lon
        prev_valid_lat = last_valid_lat
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