'''
灵敢足迹格式转换成人生点点格式（一生足迹）
'''
import csv
import math
from pathlib import Path

# ================= Config =================
INPUT_FILE = "output/gps_data_perfect.csv"
OUTPUT_DIR = "output"
OUTPUT_SUFFIX = "_converted"
CONVERT_CHINA_TO_GCJ02 = False
# =========================================


def out_of_china(lat: float, lon: float) -> bool:
    return lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if out_of_china(lat, lon):
        return lon, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    pi = math.pi

    def transform_lat(x: float, y: float) -> float:
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * pi) + 320.0 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
        return ret

    def transform_lon(x: float, y: float) -> float:
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
        return ret

    dlat = transform_lat(lon - 105.0, lat - 35.0)
    dlon = transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * pi)
    mglat = lat + dlat
    mglon = lon + dlon
    return mglon, mglat


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def convert_row(row: dict) -> dict:
    raw_lon = _to_float(row.get("longitude"))
    raw_lat = _to_float(row.get("latitude"))
    if CONVERT_CHINA_TO_GCJ02 and raw_lon is not None and raw_lat is not None:
        lon, lat = wgs84_to_gcj02(raw_lon, raw_lat)
    else:
        lon, lat = row.get("longitude", ""), row.get("latitude", "")

    return {
        "dataTime": row.get("geoTime", ""),
        "locType": row.get("locationType", ""),
        "longitude": lon,
        "latitude": lat,
        "heading": row.get("course", ""),
        "accuracy": row.get("horizontalAccuracy", ""),
        "speed": row.get("speed", ""),
        "distance": "0",
        "isBackForeground": "0",
        "stepType": "0",
        "altitude": row.get("altitude", ""),
    }


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    input_path = base_dir / INPUT_FILE
    output_dir = base_dir / OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / f"{input_path.stem}{OUTPUT_SUFFIX}.csv"

    with input_path.open("r", encoding="utf-8", newline="") as infile:
        reader = csv.DictReader(infile)
        fieldnames = [
            "dataTime",
            "locType",
            "longitude",
            "latitude",
            "heading",
            "accuracy",
            "speed",
            "distance",
            "isBackForeground",
            "stepType",
            "altitude",
        ]
        with output_path.open("w", encoding="utf-8", newline="") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                writer.writerow(convert_row(row))

    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
