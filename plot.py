'''
GPS 轨迹数据可视化脚本

功能说明:
  - 读取包含 GPS 坐标的 CSV 文件
  - 在交互式地图上绘制轨迹路线
  - 标记起点和终点位置
  
技术栈:
  - 地图库: Folium (基于 Leaflet.js)
  - 地图数据源: OpenStreetMap (OSM)
  - 坐标系: WGS-84 (GPS 标准坐标系)
  - 输出格式: 交互式 HTML 网页
'''

import pandas as pd
import folium

# ================= 配置 =================
INPUT_FILE = './修复后的数据 a.csv'
#INPUT_FILE = './output/gps_data_perfect.csv'
OUTPUT_HTML = 'trajectory_before_after.html'
# =======================================

def visualize_before_after(file_path):
    print("正在读取数据...")
    df = pd.read_csv(file_path, low_memory=False)

    # 检查必需的列
    if 'latitude' not in df.columns or 'longitude' not in df.columns:
        raise ValueError("缺少列: latitude 或 longitude")

    # 检查是否有清洁数据列
    has_clean = 'clean_latitude' in df.columns and 'clean_longitude' in df.columns

    # 过滤无效点
    df_raw = df.dropna(subset=['latitude', 'longitude'])
    
    if has_clean:
        df_clean = df.dropna(subset=['clean_latitude', 'clean_longitude'])
        center_lat = df_clean.iloc[0]['clean_latitude']
        center_lon = df_clean.iloc[0]['clean_longitude']
    else:
        center_lat = df_raw.iloc[0]['latitude']
        center_lon = df_raw.iloc[0]['longitude']

    # 创建地图
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=15,
        control_scale=True
    )

    # ================= 原始轨迹（红色） =================
    raw_route = list(zip(df_raw['latitude'], df_raw['longitude']))

    folium.PolyLine(
        raw_route,
        color='red',
        weight=3,
        opacity=0.6,
        tooltip='原始轨迹（混合坐标系）'
    ).add_to(m)

    # ================= 修复轨迹（仅当有清洁数据时） =================
    if has_clean:
        clean_route = list(zip(df_clean['clean_latitude'], df_clean['clean_longitude']))

        folium.PolyLine(
            clean_route,
            color='blue',
            weight=4,
            opacity=0.9,
            tooltip='修复后轨迹（WGS-84）'
        ).add_to(m)

        # ================= 修复后轨迹点（带 geoTime） =================
        for _, row in df_clean.iterrows():
            folium.CircleMarker(
                location=[row['clean_latitude'], row['clean_longitude']],
                radius=3,
                color='blue',
                fill=True,
                fill_opacity=0.8,
                tooltip=f"geoTime: {row['geoTime']}"
            ).add_to(m)

        # ================= 起点 & 终点（修复后） =================
        folium.Marker(
            clean_route[0],
            popup='起点（修复后）',
            icon=folium.Icon(color='green', icon='play')
        ).add_to(m)

        folium.Marker(
            clean_route[-1],
            popup='终点（修复后）',
            icon=folium.Icon(color='red', icon='stop')
        ).add_to(m)

    # 保存
    m.save(OUTPUT_HTML)

    print("-" * 40)
    print("对比可视化完成")
    print(f"输出文件: {OUTPUT_HTML}")
    print("红色线  = 原始轨迹（坐标系混用）")
    if has_clean:
        print("蓝色线  = 修复后轨迹（统一 WGS-84）")
    else:
        print("（未检测到清洁数据列）")
    print("-" * 40)


if __name__ == '__main__':
    visualize_before_after(INPUT_FILE)