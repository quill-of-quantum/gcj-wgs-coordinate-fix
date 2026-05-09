"""
CSV 坐标系转换脚本

支持两种转换方向：
1. WGS-84 -> GCJ-02
2. GCJ-02 -> WGS-84

"""
from __future__ import annotations

import csv
import math
from pathlib import Path


# ==================== 配置区（都放这里） ====================
INPUT_FILE = "./output/gps_data_manual_export.csv"
OUTPUT_FILE = "./output/gps_data_manual_export_wgs84_to_gcj02.csv"

# 可选值：
# - "wgs84_to_gcj02"
# - "gcj02_to_wgs84"
CONVERT_MODE = "wgs84_to_gcj02"

# 输入经纬度列名
LATITUDE_COLUMN = "latitude"
LONGITUDE_COLUMN = "longitude"

# 输出方式：
# True  = 覆盖原始 latitude / longitude 列
# False = 保留原列，并写入新列
OVERWRITE_SOURCE_COLUMNS = True

# 当 OVERWRITE_SOURCE_COLUMNS = False 时生效
OUTPUT_LATITUDE_COLUMN = "converted_latitude"
OUTPUT_LONGITUDE_COLUMN = "converted_longitude"

# CSV 编码
INPUT_ENCODING = "utf-8"
OUTPUT_ENCODING = "utf-8"
# =========================================================


def out_of_china(lat: float, lon: float) -> bool:
    return lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271


def _transform_lat(x: float, y: float) -> float:
    pi = math.pi
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * pi) + 320.0 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    pi = math.pi
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if out_of_china(lat, lon):
        return lon, lat

    a = 6378245.0
    ee = 0.00669342162296594323
    pi = math.pi

    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * pi)
    mglat = lat + dlat
    mglon = lon + dlon
    return mglon, mglat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    if out_of_china(lat, lon):
        return lon, lat

    a = 6378245.0
    ee = 0.00669342162296594323
    pi = math.pi

    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * pi)
    mglat = lat + dlat
    mglon = lon + dlon
    return lon * 2 - mglon, lat * 2 - mglat


def to_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def format_coord(value: float) -> str:
    return f"{value:.12f}".rstrip("0").rstrip(".")


def convert_coordinates(lon: float, lat: float) -> tuple[float, float]:
    if CONVERT_MODE == "wgs84_to_gcj02":
        return wgs84_to_gcj02(lon, lat)
    if CONVERT_MODE == "gcj02_to_wgs84":
        return gcj02_to_wgs84(lon, lat)
    raise ValueError(f"不支持的 CONVERT_MODE: {CONVERT_MODE}")


def build_fieldnames(reader_fieldnames: list[str]) -> list[str]:
    if OVERWRITE_SOURCE_COLUMNS:
        return reader_fieldnames

    fieldnames = list(reader_fieldnames)
    if OUTPUT_LATITUDE_COLUMN not in fieldnames:
        fieldnames.append(OUTPUT_LATITUDE_COLUMN)
    if OUTPUT_LONGITUDE_COLUMN not in fieldnames:
        fieldnames.append(OUTPUT_LONGITUDE_COLUMN)
    return fieldnames


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    input_path = base_dir / INPUT_FILE
    output_path = base_dir / OUTPUT_FILE
    output_path.parent.mkdir(parents=True, exist_ok=True)

    converted_count = 0
    skipped_count = 0

    with input_path.open("r", encoding=INPUT_ENCODING, newline="") as infile:
        reader = csv.DictReader(infile)
        if reader.fieldnames is None:
            raise ValueError("输入 CSV 缺少表头。")
        if LATITUDE_COLUMN not in reader.fieldnames or LONGITUDE_COLUMN not in reader.fieldnames:
            raise ValueError(
                f"输入 CSV 缺少坐标列，当前配置为: {LATITUDE_COLUMN=} {LONGITUDE_COLUMN=}"
            )

        fieldnames = build_fieldnames(reader.fieldnames)

        with output_path.open("w", encoding=OUTPUT_ENCODING, newline="") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()

            for row in reader:
                raw_lat = to_float(row.get(LATITUDE_COLUMN))
                raw_lon = to_float(row.get(LONGITUDE_COLUMN))

                if raw_lat is None or raw_lon is None:
                    skipped_count += 1
                    if not OVERWRITE_SOURCE_COLUMNS:
                        row[OUTPUT_LATITUDE_COLUMN] = ""
                        row[OUTPUT_LONGITUDE_COLUMN] = ""
                    writer.writerow(row)
                    continue

                new_lon, new_lat = convert_coordinates(raw_lon, raw_lat)
                new_lat_str = format_coord(new_lat)
                new_lon_str = format_coord(new_lon)

                if OVERWRITE_SOURCE_COLUMNS:
                    row[LATITUDE_COLUMN] = new_lat_str
                    row[LONGITUDE_COLUMN] = new_lon_str
                else:
                    row[OUTPUT_LATITUDE_COLUMN] = new_lat_str
                    row[OUTPUT_LONGITUDE_COLUMN] = new_lon_str

                writer.writerow(row)
                converted_count += 1

    print(f"转换模式: {CONVERT_MODE}")
    print(f"输入文件: {input_path}")
    print(f"输出文件: {output_path}")
    print(f"成功转换: {converted_count} 行")
    print(f"跳过无效坐标: {skipped_count} 行")


if __name__ == "__main__":
    main()
