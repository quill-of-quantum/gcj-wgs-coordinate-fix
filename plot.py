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
from folium.plugins import FastMarkerCluster, TimestampedGeoJson

# ================= 配置 =================
INPUT_FILE = './output/gps_data_perfect.csv'
#INPUT_FILE = './output/gps_data_perfect.csv'
OUTPUT_HTML = 'trajectory_before_after.html'
# 画点配置（不抽稀）
USE_POINT_MARKERS = False
USE_FAST_MARKER_CLUSTER = False
POINT_TOOLTIP = False
# 时间切片渲染配置（不抽稀，只控制显示窗口）
USE_TIME_SEGMENTS = False
TIME_COLUMN = 'geoTime'
TIME_SEGMENT_SECONDS = 300
TIME_STEP_SECONDS = None
# =======================================

def _normalize_time_series(series: pd.Series) -> pd.Series:
    series = series.dropna()
    if series.empty:
        return series
    max_val = series.max()
    if max_val > 1e12:
        return pd.to_datetime(series, unit='ms', errors='coerce')
    if max_val > 1e10:
        return pd.to_datetime(series, unit='ms', errors='coerce')
    return pd.to_datetime(series, unit='s', errors='coerce')


def _build_time_features(df: pd.DataFrame, lat_col: str, lon_col: str, time_col: str, segment_seconds: int) -> dict:
    df = df[[time_col, lat_col, lon_col]].dropna()
    df = df.sort_values(by=time_col)
    df['__ts'] = _normalize_time_series(df[time_col])
    df = df.dropna(subset=['__ts'])
    if df.empty:
        return {"type": "FeatureCollection", "features": []}

    epoch = df['__ts'].astype('int64') // 10**9
    df['__seg'] = (epoch // segment_seconds).astype(int)

    features = []
    for _, group in df.groupby('__seg'):
        coords = list(zip(group[lon_col], group[lat_col]))
        times = group['__ts'].dt.strftime('%Y-%m-%dT%H:%M:%S').tolist()
        if len(coords) < 2:
            continue
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords
            },
            "properties": {
                "times": times
            }
        })

    return {"type": "FeatureCollection", "features": features}

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
        control_scale=True,
        prefer_canvas=True
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

        if USE_TIME_SEGMENTS and TIME_COLUMN in df_clean.columns:
            time_geojson = _build_time_features(
                df_clean,
                lat_col='clean_latitude',
                lon_col='clean_longitude',
                time_col=TIME_COLUMN,
                segment_seconds=TIME_SEGMENT_SECONDS
            )

            if TIME_STEP_SECONDS is None:
                ts = _normalize_time_series(df_clean[TIME_COLUMN])
                ts = ts.dropna().sort_values()
                step = ts.diff().dt.total_seconds().dropna()
                step_seconds = int(step.median()) if not step.empty else 1
            else:
                step_seconds = int(TIME_STEP_SECONDS)

            TimestampedGeoJson(
                time_geojson,
                period=f"PT{max(step_seconds, 1)}S",
                add_last_point=False,
                auto_play=False,
                loop=False,
                max_speed=1
            ).add_to(m)
        else:
            folium.PolyLine(
                clean_route,
                color='blue',
                weight=4,
                opacity=0.9,
                tooltip='修复后轨迹（WGS-84）'
            ).add_to(m)

        # ================= 修复后轨迹点（带 geoTime） =================
        if USE_POINT_MARKERS:
            points = df_clean[['clean_latitude', 'clean_longitude', 'geoTime']].values.tolist()
            if USE_FAST_MARKER_CLUSTER:
                if POINT_TOOLTIP:
                    callback = (
                        "function (row) {"
                        "var marker = L.circleMarker(new L.LatLng(row[0], row[1]), "
                        "{radius:3, color:'blue', fill:true, fillOpacity:0.8});"
                        "marker.bindTooltip('geoTime: ' + row[2]);"
                        "return marker;"
                        "}"
                    )
                    FastMarkerCluster(points, callback=callback).add_to(m)
                else:
                    FastMarkerCluster(points).add_to(m)
            else:
                for _, row in df_clean.iterrows():
                    folium.CircleMarker(
                        location=[row['clean_latitude'], row['clean_longitude']],
                        radius=3,
                        color='blue',
                        fill=True,
                        fill_opacity=0.8,
                        tooltip=f"geoTime: {row['geoTime']}" if POINT_TOOLTIP else None
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
