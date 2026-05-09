"""
Desktop GUI with embedded map to manually adjust trajectory coordinates.

Features:
- Load a processed CSV (run.py output).
- Box-select points on a Leaflet map (inside a PyQt6 window).
- Filter view by time window around selection (default +/-12h or custom).
- Convert selected points between GCJ-02 and WGS-84.
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from PyQt6.QtCore import QObject, pyqtSlot
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebChannel import QWebChannel
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "PyQt6 and PyQt6-WebEngine are required. Install with: pip install PyQt6 PyQt6-WebEngine"
    ) from exc


# Rendering limits to avoid huge payloads in browser
MAX_RENDER_POINTS = 30000
MAX_RENDER_LINE_POINTS = 50000
MAX_SEGMENT_DISTANCE_METERS = 1000.0
MODIFIED_EPSILON = 1e-9


# --- Coordinate transforms ---

def _out_of_china(lat: float, lon: float) -> bool:
    return lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
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


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    pi = 3.1415926535897932384626
    a = 6378245.0
    ee = 0.00669342162296594323

    def transform_lat(x: float, y: float) -> float:
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * pi) + 320 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
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
    return lon * 2 - mglon, lat * 2 - mglat


# --- Data helpers ---

def _normalize_time_series(series: pd.Series) -> pd.Series:
    series = series.dropna()
    if series.empty:
        return series
    max_val = series.max()
    if max_val > 1e12:
        return pd.to_datetime(series, unit="ms", errors="coerce")
    if max_val > 1e10:
        return pd.to_datetime(series, unit="ms", errors="coerce")
    return pd.to_datetime(series, unit="s", errors="coerce")


def _sample_indices(n: int, max_n: int) -> np.ndarray:
    if max_n <= 0 or n <= max_n:
        return np.arange(n)
    return np.linspace(0, n - 1, num=max_n, dtype=int)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class AppState:
    df: pd.DataFrame
    lon_col: str
    lat_col: str
    time_col: str
    dt_series: pd.Series | None
    selected_idx: set[int] = field(default_factory=set)
    view_mask: pd.Series | None = None
    input_path: Path | None = None
    output_path: Path | None = None
    modified_idx: set[int] = field(default_factory=set)
    history: list[list[dict[str, Any]]] = field(default_factory=list)
    modified_idx: set[int] = field(default_factory=set)
    history: list[list[dict[str, Any]]] = field(default_factory=list)

    def get_view_df(self) -> pd.DataFrame:
        if self.view_mask is None:
            return self.df
        return self.df[self.view_mask]

    def update_view_all(self) -> None:
        self.view_mask = pd.Series([True] * len(self.df))


HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Trajectory Editor</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css" />
  <style>
    html, body { height: 100%; margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    #map { height: 100%; }
    .panel {
      position: absolute; top: 12px; left: 12px; z-index: 999;
      background: rgba(255,255,255,0.95); padding: 10px 12px; border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.2); width: 320px;
    }
    .panel h3 { margin: 0 0 8px; font-size: 16px; }
    .panel label { display: inline-block; width: 70px; }
    .panel input { width: 60px; }
    .panel button { margin: 4px 2px; }
    .status { font-size: 12px; color: #333; margin-top: 6px; }
    .note { font-size: 12px; color: #666; margin-top: 4px; }
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <h3>Trajectory Editor</h3>
    <div>
      <button onclick="convert('gcj2wgs')">GCJ-02 -> WGS-84</button>
      <button onclick="convert('wgs2gcj')">WGS-84 -> GCJ-02</button>
    </div>
    <div>
      <button onclick="undo()">Undo</button>
    </div>
    <div style="margin-top:6px;">
      <label>Before(h)</label>
      <input id="before" type="number" value="12" step="1" />
      <label style="width:55px;">After(h)</label>
      <input id="after" type="number" value="12" step="1" />
    </div>
    <div>
      <button onclick="applyWindow()">Apply Window</button>
      <button onclick="show12h()">Show ±12h</button>
      <button onclick="showAll()">Show All</button>
    </div>
    <div>
      <button onclick="clearSelection()">Clear Selection</button>
      <button onclick="saveFile()">Save</button>
      <button onclick="saveAs()">Save As</button>
    </div>
    <div class="status" id="statusText"></div>
    <div class="note">Use the rectangle tool to box-select points.</div>
  </div>

  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
  <script>
    const map = L.map('map');
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    let backend = null;
    let pointsLayer = null;
    let routeLines = [];
    let originalLines = [];

    function pointStyle(feature) {
      return {
        radius: feature.properties.selected ? 5 : 3,
        color: feature.properties.selected ? '#f58518' : '#2ca02c',
        fillColor: feature.properties.selected ? '#f58518' : '#2ca02c',
        fillOpacity: 0.8,
        weight: 1
      };
    }

    function renderData(payload) {
      if (!payload) return;
      const statusText = document.getElementById('statusText');
      statusText.textContent = payload.status_text || '';

      if (routeLines.length) {
        routeLines.forEach(line => map.removeLayer(line));
        routeLines = [];
      }
      if (originalLines.length) {
        originalLines.forEach(line => map.removeLayer(line));
        originalLines = [];
      }
      if (pointsLayer) map.removeLayer(pointsLayer);

      const routeSegments = payload.route_segments || [];
      const originalSegments = payload.original_segments || [];
      const pointFeatures = payload.point_features || {type: 'FeatureCollection', features: []};

      if (routeSegments.length > 0) {
        routeSegments.forEach(seg => {
          if (seg.length < 2) return;
          const line = L.polyline(seg, {color: '#1f77b4', weight: 4, opacity: 0.9});
          line.addTo(map);
          routeLines.push(line);
        });
        originalSegments.forEach(seg => {
          if (seg.length < 2) return;
          const line = L.polyline(seg, {color: '#d62728', weight: 3, opacity: 0.7});
          line.addTo(map);
          originalLines.push(line);
        });
        if (routeLines.length > 0 || originalLines.length > 0) {
          const group = L.featureGroup([...routeLines, ...originalLines]);
          map.fitBounds(group.getBounds(), {padding: [30, 30]});
        }
      } else {
        map.setView([payload.center_lat || 0, payload.center_lon || 0], 12);
      }

      pointsLayer = L.geoJSON(pointFeatures, {
        pointToLayer: function (feature, latlng) {
          return L.circleMarker(latlng, pointStyle(feature));
        }
      }).addTo(map);
    }

    function refresh() {
      if (!backend) return;
      backend.requestData(function(payload) {
        renderData(payload);
      });
    }

    new QWebChannel(qt.webChannelTransport, function(channel) {
      backend = channel.objects.backend;
      refresh();
    });

    const drawnItems = new L.FeatureGroup();
    map.addLayer(drawnItems);
    const drawControl = new L.Control.Draw({
      draw: {
        polygon: false,
        polyline: false,
        circle: false,
        marker: false,
        circlemarker: false,
        rectangle: true
      },
      edit: { featureGroup: drawnItems, edit: false, remove: true }
    });
    map.addControl(drawControl);

    map.on(L.Draw.Event.CREATED, function (e) {
      drawnItems.clearLayers();
      drawnItems.addLayer(e.layer);
      const bounds = e.layer.getBounds();
      if (!backend) return;
      backend.selectBounds(
        bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast(),
        function() { refresh(); }
      );
    });

    function applyWindow() {
      const before = parseFloat(document.getElementById('before').value || '0');
      const after = parseFloat(document.getElementById('after').value || '0');
      if (!backend) return;
      backend.applyWindow(before, after, function() { refresh(); });
    }

    function show12h() {
      document.getElementById('before').value = 12;
      document.getElementById('after').value = 12;
      applyWindow();
    }

    function showAll() {
      if (!backend) return;
      backend.showAll(function() { refresh(); });
    }

    function clearSelection() {
      if (!backend) return;
      backend.clearSelection(function() { refresh(); });
    }

    function convert(direction) {
      if (!backend) return;
      backend.convert(direction, function() { refresh(); });
    }

    function undo() {
      if (!backend) return;
      backend.undo(function() { refresh(); });
    }

    function saveFile() {
      if (!backend) return;
      backend.save(function(result) {
        if (result && result.path) {
          alert('Saved: ' + result.path);
        }
      });
    }

    function saveAs() {
      if (!backend) return;
      backend.saveAs(function(result) {
        if (result && result.path) {
          alert('Saved: ' + result.path);
        }
      });
    }
  </script>
</body>
</html>
"""


class Backend(QObject):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state

    def _status_text(self) -> str:
        total = len(self.state.df)
        view_count = len(self.state.get_view_df())
        selected = len(self.state.selected_idx)
        cols = f"lon/lat: {self.state.lon_col}/{self.state.lat_col}"
        name = self.state.input_path.name if self.state.input_path else ""
        return f"Total: {total} | View: {view_count} | Selected: {selected} | {cols} | {name}"

    def _serialize_points(self, df_view: pd.DataFrame) -> dict:
        features = []
        idx_list = df_view.index.to_numpy()
        sample_idx = _sample_indices(len(idx_list), MAX_RENDER_POINTS)
        for i in sample_idx:
            idx = int(idx_list[i])
            row = df_view.loc[idx]
            lon = row[self.state.lon_col]
            lat = row[self.state.lat_col]
            if pd.isna(lon) or pd.isna(lat):
                continue
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "idx": int(idx),
                        "selected": int(idx) in self.state.selected_idx,
                    },
                    "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                }
            )
        return {"type": "FeatureCollection", "features": features}

    def _is_modified(self, idx: int) -> bool:
        row = self.state.df.loc[idx]
        lon = row[self.state.lon_col]
        lat = row[self.state.lat_col]
        orig_lon = row["_orig_lon"]
        orig_lat = row["_orig_lat"]
        if pd.isna(lon) or pd.isna(lat) or pd.isna(orig_lon) or pd.isna(orig_lat):
            return False
        return (
            abs(float(lon) - float(orig_lon)) > MODIFIED_EPSILON
            or abs(float(lat) - float(orig_lat)) > MODIFIED_EPSILON
        )

    def _route_segments(
        self,
        df_view: pd.DataFrame,
        lon_col: str,
        lat_col: str,
        limit_idx: set[int] | None = None,
    ) -> list[list[list[float]]]:
        segments: list[list[list[float]]] = []
        current: list[list[float]] = []
        idx_list = df_view.index.to_numpy()
        sample_idx = _sample_indices(len(idx_list), MAX_RENDER_LINE_POINTS)
        prev_lat = prev_lon = None
        for i in sample_idx:
            idx = int(idx_list[i])
            if limit_idx is not None and idx not in limit_idx:
                continue
            row = df_view.loc[idx]
            lon = row[lon_col]
            lat = row[lat_col]
            if pd.isna(lon) or pd.isna(lat):
                continue
            lat_f = float(lat)
            lon_f = float(lon)
            if prev_lat is not None:
                gap = _haversine_m(prev_lat, prev_lon, lat_f, lon_f)
                if gap > MAX_SEGMENT_DISTANCE_METERS:
                    if len(current) >= 2:
                        segments.append(current)
                    current = []
            current.append([lat_f, lon_f])
            prev_lat, prev_lon = lat_f, lon_f
        if len(current) >= 2:
            segments.append(current)
        return segments

    @pyqtSlot(result="QVariant")
    def requestData(self) -> dict:
        df_view = self.state.get_view_df()
        if df_view.empty:
            center_lat, center_lon = 0.0, 0.0
        else:
            first = df_view.iloc[0]
            center_lat, center_lon = float(first[self.state.lat_col]), float(first[self.state.lon_col])

        original_segments = []
        if self.state.modified_idx:
            original_segments = self._route_segments(
                df_view,
                lon_col="_orig_lon",
                lat_col="_orig_lat",
                limit_idx=self.state.modified_idx,
            )

        return {
            "route_segments": self._route_segments(
                df_view,
                lon_col=self.state.lon_col,
                lat_col=self.state.lat_col,
            ),
            "original_segments": original_segments,
            "point_features": self._serialize_points(df_view),
            "center_lat": center_lat,
            "center_lon": center_lon,
            "status_text": self._status_text(),
        }

    @pyqtSlot(float, float, float, float, result="QVariant")
    def selectBounds(self, south: float, west: float, north: float, east: float) -> dict:
        df_view = self.state.get_view_df()
        lons = df_view[self.state.lon_col]
        lats = df_view[self.state.lat_col]
        mask = (lons >= west) & (lons <= east) & (lats >= south) & (lats <= north)
        self.state.selected_idx = set(df_view[mask].index.tolist())
        return {"ok": True, "selected": len(self.state.selected_idx)}

    @pyqtSlot(result="QVariant")
    def clearSelection(self) -> dict:
        self.state.selected_idx = set()
        return {"ok": True}

    @pyqtSlot(float, float, result="QVariant")
    def applyWindow(self, before_hours: float, after_hours: float) -> dict:
        if self.state.dt_series is None:
            return {"ok": False, "error": "geoTime not available"}
        if not self.state.selected_idx:
            return {"ok": False, "error": "no selection"}

        times = self.state.dt_series.loc[list(self.state.selected_idx)].dropna()
        if times.empty:
            return {"ok": False, "error": "invalid times"}

        tmin = times.min() - timedelta(hours=before_hours)
        tmax = times.max() + timedelta(hours=after_hours)
        self.state.view_mask = (self.state.dt_series >= tmin) & (self.state.dt_series <= tmax)
        return {"ok": True}

    @pyqtSlot(result="QVariant")
    def showAll(self) -> dict:
        self.state.update_view_all()
        return {"ok": True}

    @pyqtSlot(str, result="QVariant")
    def convert(self, direction: str) -> dict:
        if not self.state.selected_idx:
            return {"ok": False, "error": "no selection"}
        if direction not in {"gcj2wgs", "wgs2gcj"}:
            return {"ok": False, "error": "invalid direction"}

        if "manual_note" not in self.state.df.columns:
            self.state.df["manual_note"] = ""

        history_batch: list[dict[str, Any]] = []
        now_tag = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for idx in self.state.selected_idx:
            lon = self.state.df.at[idx, self.state.lon_col]
            lat = self.state.df.at[idx, self.state.lat_col]
            if pd.isna(lon) or pd.isna(lat):
                continue
            history_batch.append(
                {
                    "idx": idx,
                    "lon": lon,
                    "lat": lat,
                    "note": self.state.df.at[idx, "manual_note"],
                    "was_modified": idx in self.state.modified_idx,
                }
            )
            if direction == "gcj2wgs":
                new_lon, new_lat = gcj02_to_wgs84(float(lon), float(lat))
                note = f"manual:GCJ->WGS@{now_tag}"
            else:
                new_lon, new_lat = wgs84_to_gcj02(float(lon), float(lat))
                note = f"manual:WGS->GCJ@{now_tag}"

            self.state.df.at[idx, self.state.lon_col] = new_lon
            self.state.df.at[idx, self.state.lat_col] = new_lat
            prev = self.state.df.at[idx, "manual_note"]
            self.state.df.at[idx, "manual_note"] = f"{prev} | {note}" if prev else note
            self.state.modified_idx.add(idx)

        if history_batch:
            self.state.history.append(history_batch)
        return {"ok": True}

    @pyqtSlot(result="QVariant")
    def undo(self) -> dict:
        if not self.state.history:
            return {"ok": False, "error": "no history"}
        batch = self.state.history.pop()
        for item in batch:
            idx = item["idx"]
            self.state.df.at[idx, self.state.lon_col] = item["lon"]
            self.state.df.at[idx, self.state.lat_col] = item["lat"]
            self.state.df.at[idx, "manual_note"] = item["note"]
            if item["was_modified"]:
                self.state.modified_idx.add(idx)
            else:
                if self._is_modified(idx):
                    self.state.modified_idx.add(idx)
                else:
                    self.state.modified_idx.discard(idx)
        return {"ok": True}
    @pyqtSlot(result="QVariant")
    def save(self) -> dict:
        output_path = self.state.output_path or Path("output/gps_data_manual.csv")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.state.df.to_csv(output_path, index=False)
        return {"ok": True, "path": str(output_path)}

    @pyqtSlot(result="QVariant")
    def saveAs(self) -> dict:
        file_path, _ = QFileDialog.getSaveFileName(
            None, "Save CSV", "gps_data_manual.csv", "CSV Files (*.csv)"
        )
        if not file_path:
            return {"ok": False}
        out = Path(file_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.state.df.to_csv(out, index=False)
        return {"ok": True, "path": str(out)}


def load_state(input_path: Path, output_path: Path | None) -> AppState:
    df = pd.read_csv(input_path, low_memory=False).reset_index(drop=True)

    if "clean_longitude" in df.columns and "clean_latitude" in df.columns:
        lon_col, lat_col = "clean_longitude", "clean_latitude"
    elif "longitude" in df.columns and "latitude" in df.columns:
        lon_col, lat_col = "longitude", "latitude"
    else:
        raise SystemExit("Missing longitude/latitude columns.")

    time_col = "geoTime"
    if time_col in df.columns:
        dt_series = _normalize_time_series(df[time_col])
    else:
        dt_series = None

    if "_orig_lon" not in df.columns:
        df["_orig_lon"] = df[lon_col]
    if "_orig_lat" not in df.columns:
        df["_orig_lat"] = df[lat_col]

    state = AppState(
        df=df,
        lon_col=lon_col,
        lat_col=lat_col,
        time_col=time_col,
        dt_series=dt_series,
        input_path=input_path,
        output_path=output_path,
    )
    state.update_view_all()
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Trajectory manual editor (PyQt6)")
    parser.add_argument("--input", "-i", type=str, help="input CSV from run.py")
    parser.add_argument("--output", "-o", type=str, help="output CSV path")
    args = parser.parse_args()

    app = QApplication(sys.argv)

    if args.input:
        input_path = Path(args.input)
    else:
        file_path, _ = QFileDialog.getOpenFileName(
            None, "Open CSV", str(Path.cwd()), "CSV Files (*.csv)"
        )
        if not file_path:
            raise SystemExit("No input file selected.")
        input_path = Path(file_path)

    output_path = Path(args.output) if args.output else None

    state = load_state(input_path, output_path)
    view = QWebEngineView()
    backend = Backend(state)
    channel = QWebChannel()
    channel.registerObject("backend", backend)
    view.page().setWebChannel(channel)
    view.setHtml(HTML)
    view.setWindowTitle("Trajectory Manual Editor")
    view.resize(1200, 800)
    view.show()
    app.exec()


if __name__ == "__main__":
    main()
