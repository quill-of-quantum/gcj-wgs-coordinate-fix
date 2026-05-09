"""
Manual GPS repair GUI with SQLite-backed origin/edited/output stores.

Interaction modes:
- Line repair: choose a timezone, click a line or point to select all points on that local day,
  then convert the highlighted day between GCJ-02 and WGS-84.
- Point repair: box-select points or Cmd/Ctrl-click points for multi-select, then convert only
  the selected points.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    from PyQt6.QtCore import QObject, pyqtSlot
    from PyQt6.QtGui import QCloseEvent
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "PyQt6 and PyQt6-WebEngine are required. Install with: pip install PyQt6 PyQt6-WebEngine"
    ) from exc


MAX_SEGMENT_DISTANCE_METERS = 1000.0
MODIFIED_EPSILON = 1e-9
MAX_VIEWPORT_POINTS_POINT_MODE = 8000
MAX_VIEWPORT_POINTS_LINE_MODE = 1500
MAX_VIEWPORT_LINE_POINTS = 20000
AUTO_SMOOTH_MIN_IMPROVEMENT_RATIO = 0.08
AUTO_SMOOTH_MIN_SINGLE_DETOUR_RATIO = 1.12
AUTO_SMOOTH_MIN_PAIR_DETOUR_RATIO = 1.10
AUTO_SMOOTH_MAX_BALANCE_RATIO = 0.8
AUTO_SMOOTH_STRONG_IMPROVEMENT_RATIO = 0.25
AUTO_REPAIR_JUMP_DETECT_THRESHOLD = 50.0
AUTO_REPAIR_SMOOTH_THRESHOLD = 800.0
AUTO_REPAIR_MIN_IMPROVEMENT = 4.0
AUTO_REPAIR_AMBIGUOUS_THRESHOLD = 120.0
AUTO_REPAIR_LOOKAHEAD_GAIN = 20.0
AUTO_REPAIR_SHARP_TURN_DEG = 60.0
AUTO_REPAIR_SHARP_GAIN_MULTIPLIER = 50.0
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_OUTPUT_PATH = Path("output/gps_data_manual_export.csv")
COMMON_TIMEZONES = [
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Singapore",
    "UTC",
    "Europe/London",
    "Europe/Paris",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
]


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
    pi = math.pi
    a = 6378245.0
    ee = 0.00669342162296594323

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
    return lon * 2 - mglon, lat * 2 - mglat


def _normalize_time_series(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().any():
        max_val = numeric.dropna().max()
        unit = "ms" if max_val > 1e10 else "s"
        return pd.to_datetime(numeric, unit=unit, errors="coerce", utc=True)
    dt = pd.to_datetime(series, errors="coerce", utc=True)
    return dt


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
    origin_df: pd.DataFrame
    lon_col: str
    lat_col: str
    time_col: Optional[str]
    dt_series: Optional[pd.Series]
    input_path: Path
    output_path: Optional[Path]
    db_dir: Path
    origin_db_path: Path
    edited_db_path: Path
    output_db_path: Path
    data_bounds: Optional[tuple[float, float, float, float]] = None
    selection_mode: str = "line"
    timezone_name: str = DEFAULT_TIMEZONE
    selected_idx: set[int] = field(default_factory=set)
    selected_day: Optional[str] = None
    view_mask: Optional[pd.Series] = None
    modified_idx: set[int] = field(default_factory=set)
    history: list[list[dict[str, Any]]] = field(default_factory=list)
    focus_active: bool = False
    focus_history_depth: int = 0
    focus_selected_idx: set[int] = field(default_factory=set)
    focus_selected_day: Optional[str] = None
    edit_revision: int = 0
    export_revision: int = 0
    data_revision: int = 0
    _cached_timezone_name: Optional[str] = None
    _cached_local_dt_series: Optional[pd.Series] = None
    _cached_local_day_series: Optional[pd.Series] = None
    _cached_day_to_indices: Optional[dict[str, set[int]]] = None
    _cached_route_key: Optional[tuple[Any, ...]] = None
    _cached_route_segments: Optional[list[dict[str, Any]]] = None

    def get_view_df(self) -> pd.DataFrame:
        if self.view_mask is None:
            return self.df
        return self.df[self.view_mask]

    def update_view_all(self) -> None:
        self.view_mask = pd.Series([True] * len(self.df), index=self.df.index)

    def update_view_indices(self, indices: set[int]) -> None:
        mask = pd.Series([False] * len(self.df), index=self.df.index)
        if indices:
            mask.loc[list(indices)] = True
        self.view_mask = mask

    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def invalidate_time_cache(self) -> None:
        self._cached_timezone_name = None
        self._cached_local_dt_series = None
        self._cached_local_day_series = None
        self._cached_day_to_indices = None
        self._cached_route_key = None
        self._cached_route_segments = None

    def invalidate_geometry_cache(self) -> None:
        self.data_revision += 1
        self._cached_route_key = None
        self._cached_route_segments = None

    def local_dt_series(self) -> Optional[pd.Series]:
        if self.dt_series is None:
            return None
        if (
            self._cached_local_dt_series is None
            or self._cached_timezone_name != self.timezone_name
        ):
            self._cached_local_dt_series = self.dt_series.dt.tz_convert(self.tzinfo())
            self._cached_timezone_name = self.timezone_name
            self._cached_local_day_series = None
            self._cached_day_to_indices = None
        return self._cached_local_dt_series

    def local_day_series(self) -> Optional[pd.Series]:
        if self.dt_series is None:
            return None
        if self._cached_local_day_series is None:
            local_dt = self.local_dt_series()
            if local_dt is None:
                return None
            self._cached_local_day_series = local_dt.dt.strftime("%Y-%m-%d")
            self._cached_day_to_indices = None
        return self._cached_local_day_series

    def day_to_indices(self) -> Optional[dict[str, set[int]]]:
        day_series = self.local_day_series()
        if day_series is None:
            return None
        if self._cached_day_to_indices is None:
            valid = day_series.dropna()
            groups = valid.groupby(valid, sort=False).groups
            self._cached_day_to_indices = {
                str(day): set(indexes.tolist()) for day, indexes in groups.items()
            }
        return self._cached_day_to_indices


HTML = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Manual GPS Repair</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    html, body {{ height: 100%; margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    #map {{ height: 100%; width: 100%; }}
    .panel {{
      position: absolute;
      top: 12px;
      left: 12px;
      z-index: 999;
      width: 360px;
      background: rgba(255, 255, 255, 0.96);
      border-radius: 12px;
      padding: 14px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.16);
    }}
    .title {{ font-size: 17px; font-weight: 600; margin-bottom: 10px; }}
    .row {{ margin-bottom: 10px; }}
    .label {{ font-size: 12px; color: #444; margin-bottom: 4px; display: block; }}
    .buttons {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    button {{
      border: 0;
      border-radius: 8px;
      padding: 8px 10px;
      background: #1f77b4;
      color: white;
      cursor: pointer;
    }}
    button.secondary {{ background: #4c566a; }}
    button.warn {{ background: #b45309; }}
    button:disabled {{ opacity: 0.55; cursor: default; }}
    select, input {{
      width: 100%;
      box-sizing: border-box;
      padding: 7px 8px;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
    }}
    .hint {{ font-size: 12px; color: #4b5563; line-height: 1.4; }}
    .status {{
      font-size: 12px;
      line-height: 1.45;
      color: #111827;
      background: #f8fafc;
      border-radius: 8px;
      padding: 8px;
      white-space: pre-line;
    }}
    .map-box-select {{
      background: rgba(37, 99, 235, 0.10);
      border: 2px solid rgba(37, 99, 235, 0.9);
    }}
    .screen-box-select {{
      position: absolute;
      border: 2px solid rgba(37, 99, 235, 0.9);
      background: rgba(37, 99, 235, 0.10);
      pointer-events: none;
      z-index: 800;
      display: none;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <div class="title">Manual GPS Repair</div>

    <div class="row">
      <span class="label">Mode</span>
      <div class="buttons">
        <button id="modeLineBtn" onclick="setMode('line')">Line Repair</button>
        <button id="modePointBtn" class="secondary" onclick="setMode('point')">Point Repair</button>
      </div>
    </div>

    <div class="row">
      <label class="label" for="timezoneSelect">Timezone</label>
      <input id="timezoneSelect" list="timezoneList" value="{DEFAULT_TIMEZONE}" />
      <datalist id="timezoneList">
        {''.join(f'<option value="{tz}"></option>' for tz in COMMON_TIMEZONES)}
      </datalist>
    </div>

    <div class="row">
      <div class="buttons">
        <button onclick="applyTimezone()">Apply Timezone</button>
        <button class="secondary" onclick="refresh()">Refresh</button>
      </div>
    </div>

    <div class="row">
      <div class="buttons">
        <button onclick="convertSelection('gcj2wgs')">GCJ-02 -> WGS-84</button>
        <button onclick="convertSelection('wgs2gcj')">WGS-84 -> GCJ-02</button>
        <button class="secondary" onclick="autoSmoothSelection()">Auto Smooth</button>
      </div>
    </div>

    <div class="row">
      <div class="buttons">
        <button class="secondary" onclick="clearSelection()">Clear Selection</button>
        <button class="secondary" onclick="undo()">Undo</button>
      </div>
    </div>

    <div class="row">
      <div class="buttons">
        <button id="focusBtn" class="secondary" onclick="enterFocus()">Focus Selected</button>
        <button id="focusSaveBtn" class="secondary" onclick="saveFocus()">Save Focus</button>
        <button id="focusCancelBtn" class="secondary" onclick="cancelFocus()">Cancel Focus</button>
      </div>
    </div>

    <div class="row">
      <div class="buttons">
        <button class="warn" onclick="saveFile()">Export CSV</button>
        <button class="warn" onclick="saveAs()">Export CSV As</button>
      </div>
    </div>

    <div class="row status" id="statusText"></div>
    <div class="hint">
      Line repair: click any line or point to select that local day.<br>
      Point repair: hold Cmd/Ctrl and drag with left mouse to box-select, or Cmd/Ctrl + click for multi-select.
    </div>
  </div>

  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet-rotate@0.2.8/dist/leaflet-rotate-src.js"></script>
  <script>
    const ROTATE_SPEED = 90;
    const map = L.map('map', {{
      preferCanvas: true,
      rotate: true,
      bearing: 0
    }});
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    let backend = null;
    let pointLayerMap = new Map();
    let lineLayerMap = new Map();
    const pointLayerGroup = L.layerGroup().addTo(map);
    const lineLayerGroup = L.layerGroup().addTo(map);
    let currentPayload = null;
    let hasFitBounds = false;
    let modifierPressed = false;
    let boxSelecting = false;
    let boxMoved = false;
    let boxStartPoint = null;
    let boxRect = null;
    let suppressNextClick = false;
    let refreshTimer = null;
    let currentRenderedFeatures = [];
    let rotationFrame = null;
    let rotationDirection = 0;
    let lastRotateTs = null;
    const pressedRotationKeys = new Set();
    const screenBox = document.createElement('div');
    screenBox.className = 'screen-box-select';
    map.getContainer().appendChild(screenBox);

    function setStatus(text) {{
      document.getElementById('statusText').textContent = text || '';
    }}

    function normalizeBearing(angle) {{
      let normalized = angle % 360;
      if (normalized < 0) normalized += 360;
      return normalized;
    }}

    function rotateMap(delta) {{
      if (!map.setBearing || !map.getBearing) return;
      map.setBearing(normalizeBearing(map.getBearing() + delta));
    }}

    function resetBearing() {{
      if (!map.setBearing) return;
      map.setBearing(0);
    }}

    function updateRotationDirection() {{
      const hasQ = pressedRotationKeys.has('q');
      const hasE = pressedRotationKeys.has('e');
      if (hasQ && !hasE) {{
        rotationDirection = -1;
      }} else if (hasE && !hasQ) {{
        rotationDirection = 1;
      }} else {{
        rotationDirection = 0;
      }}
    }}

    function stepRotation(ts) {{
      if (rotationDirection === 0) {{
        rotationFrame = null;
        lastRotateTs = null;
        return;
      }}
      if (lastRotateTs === null) {{
        lastRotateTs = ts;
      }}
      const deltaSeconds = Math.min((ts - lastRotateTs) / 1000, 0.05);
      lastRotateTs = ts;
      rotateMap(rotationDirection * ROTATE_SPEED * deltaSeconds);
      rotationFrame = window.requestAnimationFrame(stepRotation);
    }}

    function ensureRotationLoop() {{
      if (rotationFrame !== null || rotationDirection === 0) return;
      rotationFrame = window.requestAnimationFrame(stepRotation);
    }}

    function stopRotationLoop() {{
      if (rotationFrame !== null) {{
        window.cancelAnimationFrame(rotationFrame);
        rotationFrame = null;
      }}
      lastRotateTs = null;
    }}

    function isPointMode() {{
      return currentPayload && currentPayload.selection_mode === 'point';
    }}

    function clearBoxRect() {{
      boxRect = null;
      screenBox.style.display = 'none';
    }}

    function enableMapDragging() {{
      if (map.dragging && !map.dragging.enabled()) {{
        map.dragging.enable();
      }}
    }}

    function disableMapDragging() {{
      if (map.dragging && map.dragging.enabled()) {{
        map.dragging.disable();
      }}
    }}

    function updateModifierState(event, isDown) {{
      modifierPressed = isDown || !!(event.metaKey || event.ctrlKey);
      if (!modifierPressed && !boxSelecting) {{
        enableMapDragging();
      }}
    }}

    document.addEventListener('keydown', function(event) {{
      updateModifierState(event, true);
      const tagName = event.target && event.target.tagName ? event.target.tagName.toLowerCase() : '';
      if (tagName === 'input' || tagName === 'textarea' || tagName === 'select') return;
      if (event.key === 'q' || event.key === 'Q') {{
        event.preventDefault();
        pressedRotationKeys.add('q');
        updateRotationDirection();
        ensureRotationLoop();
      }} else if (event.key === 'e' || event.key === 'E') {{
        event.preventDefault();
        pressedRotationKeys.add('e');
        updateRotationDirection();
        ensureRotationLoop();
      }} else if (event.key === 'r' || event.key === 'R') {{
        event.preventDefault();
        pressedRotationKeys.clear();
        updateRotationDirection();
        stopRotationLoop();
        resetBearing();
      }}
    }});

    document.addEventListener('keyup', function(event) {{
      updateModifierState(event, false);
      if (event.key === 'q' || event.key === 'Q') {{
        pressedRotationKeys.delete('q');
        updateRotationDirection();
        if (rotationDirection === 0) stopRotationLoop();
      }} else if (event.key === 'e' || event.key === 'E') {{
        pressedRotationKeys.delete('e');
        updateRotationDirection();
        if (rotationDirection === 0) stopRotationLoop();
      }}
    }});

    window.addEventListener('blur', function() {{
      modifierPressed = false;
      pressedRotationKeys.clear();
      updateRotationDirection();
      stopRotationLoop();
      if (!boxSelecting) {{
        enableMapDragging();
      }}
    }});

    function startBoxSelection(event) {{
      if (!backend || !isPointMode()) return;
      if (!(event.originalEvent.metaKey || event.originalEvent.ctrlKey)) return;
      if (event.originalEvent.button !== 0) return;
      boxSelecting = true;
      boxMoved = false;
      boxStartPoint = event.containerPoint;
      suppressNextClick = false;
      disableMapDragging();
      clearBoxRect();
      L.DomEvent.stop(event.originalEvent);
    }}

    function updateBoxSelection(event) {{
      if (!boxSelecting || !boxStartPoint) return;
      const currentPoint = event.containerPoint;
      const dx = currentPoint.x - boxStartPoint.x;
      const dy = currentPoint.y - boxStartPoint.y;
      if (!boxMoved && (Math.abs(dx) > 3 || Math.abs(dy) > 3)) {{
        boxMoved = true;
      }}
      boxRect = {{
        left: Math.min(boxStartPoint.x, currentPoint.x),
        top: Math.min(boxStartPoint.y, currentPoint.y),
        right: Math.max(boxStartPoint.x, currentPoint.x),
        bottom: Math.max(boxStartPoint.y, currentPoint.y),
      }};
      screenBox.style.display = 'block';
      screenBox.style.left = `${{boxRect.left}}px`;
      screenBox.style.top = `${{boxRect.top}}px`;
      screenBox.style.width = `${{boxRect.right - boxRect.left}}px`;
      screenBox.style.height = `${{boxRect.bottom - boxRect.top}}px`;
    }}

    function featureIdsInScreenBox(rect) {{
      const ids = [];
      currentRenderedFeatures.forEach(feature => {{
        if (!feature.geometry || !feature.geometry.coordinates) return;
        const coords = feature.geometry.coordinates;
        const point = map.latLngToContainerPoint([coords[1], coords[0]]);
        if (
          point.x >= rect.left && point.x <= rect.right &&
          point.y >= rect.top && point.y <= rect.bottom
        ) {{
          ids.push(feature.properties.idx);
        }}
      }});
      return ids;
    }}

    function finishBoxSelection(event) {{
      if (!boxSelecting) return;
      boxSelecting = false;
      enableMapDragging();
      if (!boxMoved || !boxRect || !backend || !isPointMode()) {{
        clearBoxRect();
        boxStartPoint = null;
        return;
      }}
      suppressNextClick = true;
      const rect = boxRect;
      const selectedIds = featureIdsInScreenBox(rect);
      clearBoxRect();
      boxStartPoint = null;
      backend.selectIndices(
        selectedIds, false,
        function() {{ refresh(false); }}
      );
      if (event && event.originalEvent) {{
        L.DomEvent.stop(event.originalEvent);
      }}
    }}

    document.addEventListener('mouseup', function(event) {{
      if (!boxSelecting) return;
      const rect = map.getContainer().getBoundingClientRect();
      const containerPoint = L.point(event.clientX - rect.left, event.clientY - rect.top);
      updateBoxSelection({{ containerPoint }});
      finishBoxSelection({{ originalEvent: event }});
    }});

    function syncControls(payload) {{
      currentPayload = payload || null;
      const tzSelect = document.getElementById('timezoneSelect');
      if (payload && payload.timezone_name) {{
        tzSelect.value = payload.timezone_name;
      }}
      const isLine = !payload || payload.selection_mode === 'line';
      document.getElementById('modeLineBtn').className = isLine ? '' : 'secondary';
      document.getElementById('modePointBtn').className = isLine ? 'secondary' : '';
      const focusActive = !!(payload && payload.focus_active);
      document.getElementById('focusBtn').disabled = focusActive;
      document.getElementById('focusSaveBtn').disabled = !focusActive;
      document.getElementById('focusCancelBtn').disabled = !focusActive;
    }}

    function pointStyle(feature) {{
      const props = feature.properties || {{}};
      let color = '#2ca02c';
      let radius = 3.5;
      if (props.modified) {{
        color = '#7c3aed';
      }}
      if (props.selected) {{
        color = '#f97316';
        radius = 5.5;
      }}
      return {{
        radius,
        color,
        fillColor: color,
        fillOpacity: 0.85,
        weight: props.selected ? 2 : 1
      }};
    }}

    function lineStyle(seg) {{
      return {{
        color: seg.selected ? '#f97316' : '#2563eb',
        weight: seg.selected ? 5 : 3,
        opacity: seg.selected ? 0.95 : 0.72
      }};
    }}

    function featureTooltip(props) {{
      return [
        `idx: ${{props.idx}}`,
        props.local_time ? `time: ${{props.local_time}}` : null,
        props.local_day ? `day: ${{props.local_day}}` : null,
        props.modified ? 'modified' : null,
      ].filter(Boolean).join('\\n');
    }}

    function updateLineLayers(segments) {{
      const nextIds = new Set();
      segments.forEach(seg => {{
        const coords = seg.coords || [];
        if (coords.length < 2) return;
        const segId = seg.seg_id || `${{seg.day}}:${{coords.length}}`;
        nextIds.add(segId);
        let line = lineLayerMap.get(segId);
        if (!line) {{
          line = L.polyline(coords, lineStyle(seg));
          line.on('click', function() {{
            if (!backend) return;
            backend.selectDay(seg.day, function() {{ refresh(false); }});
          }});
          line.segId = segId;
          line.segDay = seg.day;
          line.addTo(lineLayerGroup);
          lineLayerMap.set(segId, line);
        }} else {{
          line.setLatLngs(coords);
          line.setStyle(lineStyle(seg));
          line.segDay = seg.day;
        }}
      }});
      Array.from(lineLayerMap.keys()).forEach(segId => {{
        if (nextIds.has(segId)) return;
        const line = lineLayerMap.get(segId);
        if (line) {{
          lineLayerGroup.removeLayer(line);
          lineLayerMap.delete(segId);
        }}
      }});
    }}

    function updatePointLayers(features, payload) {{
      const nextIds = new Set();
      features.forEach(feature => {{
        const props = feature.properties || {{}};
        const geom = feature.geometry || {{}};
        const coords = geom.coordinates || null;
        if (!coords || coords.length < 2) return;
        const pointId = props.idx;
        nextIds.add(pointId);
        const latlng = [coords[1], coords[0]];
        let layer = pointLayerMap.get(pointId);
        if (!layer) {{
          layer = L.circleMarker(latlng, pointStyle(feature));
          layer.on('click', function(event) {{
            if (suppressNextClick) {{
              suppressNextClick = false;
              return;
            }}
            if (!backend) return;
            const layerProps = layer.featureProps || {{}};
            if ((payload.selection_mode || 'line') === 'line') {{
              backend.selectDay(layerProps.local_day || '', function() {{ refresh(false); }});
              return;
            }}
            const e = event.originalEvent || {{}};
            const additive = !!(e.metaKey || e.ctrlKey);
            backend.togglePointSelection(layerProps.idx, additive, function() {{ refresh(false); }});
          }});
          layer.addTo(pointLayerGroup);
          pointLayerMap.set(pointId, layer);
        }}
        layer.setLatLng(latlng);
        layer.setStyle(pointStyle(feature));
        layer.featureProps = props;
        const tooltipText = featureTooltip(props);
        if (tooltipText) {{
          if (layer.getTooltip()) {{
            layer.setTooltipContent(tooltipText);
          }} else {{
            layer.bindTooltip(tooltipText);
          }}
        }}
      }});
      Array.from(pointLayerMap.keys()).forEach(pointId => {{
        if (nextIds.has(pointId)) return;
        const layer = pointLayerMap.get(pointId);
        if (layer) {{
          pointLayerGroup.removeLayer(layer);
          pointLayerMap.delete(pointId);
        }}
      }});
    }}

    function renderData(payload) {{
      syncControls(payload);
      setStatus(payload.status_text || '');
      const segments = payload.route_segments || [];
      updateLineLayers(segments);
      const geojson = payload.point_features || {{ type: 'FeatureCollection', features: [] }};
      currentRenderedFeatures = geojson.features || [];
      updatePointLayers(currentRenderedFeatures, payload);

    }}

    function refresh(resetBounds = false) {{
      if (!backend) return;
      if (resetBounds) {{
        refreshFromMeta();
        return;
      }}
      const bounds = map.getBounds();
      backend.requestDataForViewport(
        bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast(), map.getZoom(),
        function(payload) {{
        renderData(payload);
        }}
      );
    }}

    function scheduleRefresh() {{
      if (refreshTimer) {{
        clearTimeout(refreshTimer);
      }}
      refreshTimer = setTimeout(function() {{
        refresh(false);
      }}, 220);
    }}

    function refreshFromMeta() {{
      if (!backend) return;
      backend.requestMeta(function(meta) {{
        if (!meta) return;
        if (meta.data_bounds) {{
          const b = meta.data_bounds;
          map.fitBounds([[b.south, b.west], [b.north, b.east]], {{ padding: [30, 30] }});
          hasFitBounds = true;
        }} else if (meta.center_lat !== null && meta.center_lon !== null) {{
          map.setView([meta.center_lat, meta.center_lon], 12);
          hasFitBounds = true;
        }}
        refresh(false);
      }});
    }}

    function setMode(mode) {{
      if (!backend) return;
      backend.setMode(mode, function(result) {{
        if (result && result.error) alert(result.error);
        clearBoxRect();
        boxSelecting = false;
        boxStartPoint = null;
        enableMapDragging();
        refresh(false);
      }});
    }}

    function applyTimezone() {{
      if (!backend) return;
      const tz = document.getElementById('timezoneSelect').value;
      backend.setTimezone(tz, function(result) {{
        if (result && result.error) {{
          alert(result.error);
        }}
        refresh(false);
      }});
    }}

    function clearSelection() {{
      if (!backend) return;
      backend.clearSelection(function() {{ refresh(false); }});
    }}

    function enterFocus() {{
      if (!backend) return;
      backend.enterFocus(function(result) {{
        if (result && result.error) {{
          alert(result.error);
          return;
        }}
        refresh(false);
      }});
    }}

    function saveFocus() {{
      if (!backend) return;
      backend.saveFocus(function(result) {{
        if (result && result.error) {{
          alert(result.error);
          return;
        }}
        refresh(false);
      }});
    }}

    function cancelFocus() {{
      if (!backend) return;
      backend.cancelFocus(function(result) {{
        if (result && result.error) {{
          alert(result.error);
          return;
        }}
        refresh(false);
      }});
    }}

    function convertSelection(direction) {{
      if (!backend) return;
      backend.convert(direction, function(result) {{
        if (result && result.error) {{
          alert(result.error);
        }}
        refresh(false);
      }});
    }}

    function autoSmoothSelection() {{
      if (!backend) return;
      backend.autoSmoothSelection(function(result) {{
        if (result && result.error) {{
          alert(result.error);
          return;
        }}
        refresh(false);
      }});
    }}

    function undo() {{
      if (!backend) return;
      backend.undo(function(result) {{
        if (result && result.error) {{
          alert(result.error);
        }}
        refresh(false);
      }});
    }}

    function saveFile() {{
      if (!backend) return;
      backend.save(function(result) {{
        if (result && result.error) {{
          alert(result.error);
          return;
        }}
        if (result && result.path) {{
          alert('Exported: ' + result.path);
        }}
      }});
    }}

    function saveAs() {{
      if (!backend) return;
      backend.saveAs(function(result) {{
        if (result && result.error) {{
          alert(result.error);
          return;
        }}
        if (result && result.path) {{
          alert('Exported: ' + result.path);
        }}
      }});
    }}

    map.on('mousedown', startBoxSelection);
    map.on('mousemove', updateBoxSelection);
    map.on('mouseup', finishBoxSelection);
    map.on('moveend', function() {{
      if (!backend || !hasFitBounds || boxSelecting) return;
      scheduleRefresh();
    }});

    new QWebChannel(qt.webChannelTransport, function(channel) {{
      backend = channel.objects.backend;
      refreshFromMeta();
    }});
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
        modified = len(self.state.modified_idx)
        selected = len(self._active_selection())
        day_text = self.state.selected_day or "-"
        focus_text = "on" if self.state.focus_active else "off"
        export_text = "clean" if self.state.edit_revision == self.state.export_revision else "needs export"
        return (
            f"mode: {self.state.selection_mode} | timezone: {self.state.timezone_name}\n"
            f"total: {total} | modified: {modified} | selected: {selected}\n"
            f"selected day: {day_text} | focus: {focus_text} | export: {export_text}\n"
            f"origin db: {self.state.origin_db_path.name} | edited db: {self.state.edited_db_path.name}"
        )

    def _mark_edited(self) -> None:
        self.state.edit_revision += 1

    def _segment_len(self, p0: np.ndarray, p1: np.ndarray) -> float:
        return float(np.linalg.norm(p1 - p0))

    def _geo_distance_m(self, lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        r = 6371000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _turning_angle(self, p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
        lat_mid = p2[1]
        cos_lat = math.cos(math.radians(lat_mid))
        v1 = np.array([(p2[0] - p1[0]) * cos_lat, p2[1] - p1[1]], dtype=float)
        v2 = np.array([(p3[0] - p2[0]) * cos_lat, p3[1] - p2[1]], dtype=float)
        norm1 = float(np.linalg.norm(v1))
        norm2 = float(np.linalg.norm(v2))
        if norm1 == 0.0 or norm2 == 0.0:
            return 180.0
        cos_theta = float(np.dot(v1, v2) / (norm1 * norm2))
        cos_theta = max(-1.0, min(1.0, cos_theta))
        angle_math = math.degrees(math.acos(cos_theta))
        return 180.0 - angle_math

    def _point_line_distance(self, point: np.ndarray, start: np.ndarray, end: np.ndarray) -> float:
        segment = end - start
        seg_norm = float(np.dot(segment, segment))
        if seg_norm < 1e-12:
            return self._segment_len(point, start)
        t = float(np.dot(point - start, segment) / seg_norm)
        t = max(0.0, min(1.0, t))
        projection = start + t * segment
        return self._segment_len(point, projection)

    def _coord_variants(self, point: np.ndarray) -> list[tuple[str, np.ndarray]]:
        lon = float(point[0])
        lat = float(point[1])
        gcj_to_wgs = gcj02_to_wgs84(lon, lat)
        wgs_to_gcj = wgs84_to_gcj02(lon, lat)
        return [
            ("keep", np.array([lon, lat], dtype=float)),
            ("gcj2wgs", np.array([gcj_to_wgs[0], gcj_to_wgs[1]], dtype=float)),
            ("wgs2gcj", np.array([wgs_to_gcj[0], wgs_to_gcj[1]], dtype=float)),
        ]

    def _single_path_sum(self, pts: np.ndarray, idx: int, candidate: np.ndarray) -> float:
        prev_pt = pts[idx - 1]
        next_pt = pts[idx + 1]
        return self._segment_len(prev_pt, candidate) + self._segment_len(candidate, next_pt)

    def _pair_path_sum(self, pts: np.ndarray, idx: int, cand0: np.ndarray, cand1: np.ndarray) -> float:
        p0 = pts[idx - 1]
        p3 = pts[idx + 2]
        return (
            self._segment_len(p0, cand0)
            + self._segment_len(cand0, cand1)
            + self._segment_len(cand1, p3)
        )

    def _edge_balance(self, edge_lengths: list[float]) -> float:
        positive = [edge for edge in edge_lengths if edge > 1e-12]
        if len(positive) < len(edge_lengths):
            return float("inf")
        mean_val = sum(positive) / len(positive)
        if mean_val <= 1e-12:
            return float("inf")
        variance = sum((edge - mean_val) ** 2 for edge in positive) / len(positive)
        return math.sqrt(variance) / mean_val

    def _should_accept_single(self, pts: np.ndarray, idx: int, candidate: np.ndarray) -> bool:
        prev_pt = pts[idx - 1]
        cur_pt = pts[idx]
        next_pt = pts[idx + 1]
        baseline = self._segment_len(prev_pt, cur_pt) + self._segment_len(cur_pt, next_pt)
        direct = self._segment_len(prev_pt, next_pt)
        if direct < 1e-12:
            return False
        if baseline / direct < AUTO_SMOOTH_MIN_SINGLE_DETOUR_RATIO:
            return False
        new_sum = self._single_path_sum(pts, idx, candidate)
        if baseline <= 1e-12:
            return False
        improvement = (baseline - new_sum) / baseline
        baseline_balance = self._edge_balance(
            [
                self._segment_len(prev_pt, cur_pt),
                self._segment_len(cur_pt, next_pt),
            ]
        )
        candidate_balance = self._edge_balance(
            [
                self._segment_len(prev_pt, candidate),
                self._segment_len(candidate, next_pt),
            ]
        )
        if improvement < AUTO_SMOOTH_MIN_IMPROVEMENT_RATIO and candidate_balance >= baseline_balance:
            return False
        if (
            candidate_balance <= baseline_balance * AUTO_SMOOTH_MAX_BALANCE_RATIO
            and improvement > 0
        ):
            return True
        current_offset = self._point_line_distance(cur_pt, prev_pt, next_pt)
        candidate_offset = self._point_line_distance(candidate, prev_pt, next_pt)
        if candidate_offset < current_offset and improvement >= AUTO_SMOOTH_MIN_IMPROVEMENT_RATIO:
            return True
        return improvement >= AUTO_SMOOTH_STRONG_IMPROVEMENT_RATIO

    def _should_accept_pair(
        self, pts: np.ndarray, idx: int, cand0: np.ndarray, cand1: np.ndarray
    ) -> bool:
        p0 = pts[idx - 1]
        p1 = pts[idx]
        p2 = pts[idx + 1]
        p3 = pts[idx + 2]
        baseline = self._pair_path_sum(pts, idx, p1, p2)
        direct = self._segment_len(p0, p3)
        if direct < 1e-12:
            return False
        if baseline / direct < AUTO_SMOOTH_MIN_PAIR_DETOUR_RATIO:
            baseline_gate = False
        else:
            baseline_gate = True
        new_sum = self._pair_path_sum(pts, idx, cand0, cand1)
        if baseline <= 1e-12:
            return False
        improvement = (baseline - new_sum) / baseline
        baseline_balance = self._edge_balance(
            [
                self._segment_len(p0, p1),
                self._segment_len(p1, p2),
                self._segment_len(p2, p3),
            ]
        )
        candidate_balance = self._edge_balance(
            [
                self._segment_len(p0, cand0),
                self._segment_len(cand0, cand1),
                self._segment_len(cand1, p3),
            ]
        )
        if not baseline_gate:
            return (
                improvement >= AUTO_SMOOTH_STRONG_IMPROVEMENT_RATIO
                and candidate_balance <= baseline_balance * AUTO_SMOOTH_MAX_BALANCE_RATIO
            )
        if improvement < AUTO_SMOOTH_MIN_IMPROVEMENT_RATIO and candidate_balance >= baseline_balance:
            return False
        if candidate_balance <= baseline_balance * AUTO_SMOOTH_MAX_BALANCE_RATIO and improvement > 0:
            return True
        current_offset = self._point_line_distance(p1, p0, p3) + self._point_line_distance(p2, p0, p3)
        candidate_offset = self._point_line_distance(cand0, p0, p3) + self._point_line_distance(cand1, p0, p3)
        if candidate_offset < current_offset and improvement >= AUTO_SMOOTH_MIN_IMPROVEMENT_RATIO:
            return True
        return improvement >= AUTO_SMOOTH_STRONG_IMPROVEMENT_RATIO

    def _undo_last_batch(self) -> dict[str, Any]:
        if not self.state.history:
            return {"ok": False, "error": "no history"}
        batch = self.state.history.pop()
        touched: set[int] = set()
        for item in batch:
            idx = item["idx"]
            self.state.df.at[idx, self.state.lon_col] = item["lon"]
            self.state.df.at[idx, self.state.lat_col] = item["lat"]
            self.state.df.at[idx, "manual_note"] = item["note"]
            self.state.df.at[idx, "manual_timezone"] = item["timezone"]
            self.state.df.at[idx, "manual_mode"] = item["mode"]
            self.state.df.at[idx, "manual_updated_at"] = item["updated_at"]
            touched.add(idx)
        self._update_modified_flags(touched)
        self.state.invalidate_geometry_cache()
        self._persist_edited_db()
        self._mark_edited()
        return {"ok": True, "undone": len(batch)}

    def _active_selection(self) -> set[int]:
        if self.state.selection_mode == "line" and self.state.selected_day:
            return self._indices_for_day(self.state.selected_day)
        return set(self.state.selected_idx)

    def _indices_for_day(self, day: str) -> set[int]:
        day_map = self.state.day_to_indices()
        if day_map is None:
            return set(self.state.df.index) if day else set()
        return set(day_map.get(day, set()))

    def _is_modified(self, idx: int) -> bool:
        row = self.state.df.loc[idx]
        return (
            abs(float(row[self.state.lon_col]) - float(row["_orig_lon"])) > MODIFIED_EPSILON
            or abs(float(row[self.state.lat_col]) - float(row["_orig_lat"])) > MODIFIED_EPSILON
        )

    def _viewport_mask(self, south: float, west: float, north: float, east: float) -> pd.Series:
        df_view = self.state.get_view_df()
        return (
            (df_view[self.state.lon_col] >= west)
            & (df_view[self.state.lon_col] <= east)
            & (df_view[self.state.lat_col] >= south)
            & (df_view[self.state.lat_col] <= north)
        )

    def _limit_indices(self, idx_array: np.ndarray, max_count: int, keep_idx: set[int]) -> np.ndarray:
        if len(idx_array) <= max_count:
            return idx_array
        keep = np.array([idx for idx in idx_array if int(idx) in keep_idx], dtype=idx_array.dtype)
        if len(keep) >= max_count:
            keep_sample = _sample_indices(len(keep), max_count)
            return keep[keep_sample]
        remaining = np.array([idx for idx in idx_array if int(idx) not in keep_idx], dtype=idx_array.dtype)
        extra_needed = max_count - len(keep)
        if extra_needed > 0 and len(remaining) > extra_needed:
            remaining = remaining[_sample_indices(len(remaining), extra_needed)]
        return np.concatenate([keep, remaining])

    def _viewport_limits(self, zoom: float) -> tuple[int, int]:
        if zoom <= 5:
            return 800, 4000
        if zoom <= 7:
            return 1500, 7000
        if zoom <= 9:
            return 3000, 10000
        if zoom <= 11:
            return 5000, 14000
        if self.state.selection_mode == "point":
            return MAX_VIEWPORT_POINTS_POINT_MODE, MAX_VIEWPORT_LINE_POINTS
        return MAX_VIEWPORT_POINTS_LINE_MODE, MAX_VIEWPORT_LINE_POINTS

    def _serialize_points(self, df_view: pd.DataFrame, max_points: int) -> dict[str, Any]:
        features: list[dict[str, Any]] = []
        idx_list = df_view.index.to_numpy()
        local_day_series = self.state.local_day_series()
        local_dt_series = self.state.local_dt_series()
        active_selection = self._active_selection()
        sampled_indices = self._limit_indices(idx_list, max_points, active_selection)
        sampled_df = self.state.df.loc[sampled_indices, [self.state.lon_col, self.state.lat_col]]
        lon_values = sampled_df[self.state.lon_col].to_numpy()
        lat_values = sampled_df[self.state.lat_col].to_numpy()
        for pos, idx_raw in enumerate(sampled_indices):
            idx = int(idx_raw)
            lon = lon_values[pos]
            lat = lat_values[pos]
            if pd.isna(lon) or pd.isna(lat):
                continue
            local_day = ""
            local_time = ""
            if local_day_series is not None:
                val = local_day_series.at[idx]
                local_day = "" if pd.isna(val) else str(val)
            if local_dt_series is not None:
                val = local_dt_series.at[idx]
                if pd.notna(val):
                    local_time = val.strftime("%Y-%m-%d %H:%M:%S")
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "idx": idx,
                        "selected": idx in active_selection,
                        "modified": idx in self.state.modified_idx,
                        "local_day": local_day,
                        "local_time": local_time,
                    },
                    "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                }
            )
        return {"type": "FeatureCollection", "features": features}

    def _route_segments(self, df_view: pd.DataFrame, max_line_points: int) -> list[dict[str, Any]]:
        if df_view.empty:
            return []
        if len(df_view):
            first_idx = int(df_view.index[0])
            last_idx = int(df_view.index[-1])
        else:
            first_idx = last_idx = -1
        cache_key = (
            self.state.timezone_name,
            self.state.data_revision,
            len(df_view),
            first_idx,
            last_idx,
            max_line_points,
        )
        if self.state._cached_route_key != cache_key or self.state._cached_route_segments is None:
            segments: list[dict[str, Any]] = []
            local_day_series = self.state.local_day_series()
            if local_day_series is None:
                day_groups = [("all", df_view.index.to_numpy())]
            else:
                day_values = local_day_series.loc[df_view.index]
                valid_mask = day_values.notna().to_numpy()
                valid_indices = df_view.index.to_numpy()[valid_mask]
                valid_days = day_values.to_numpy()[valid_mask]
                day_groups = []
                if len(valid_indices):
                    start = 0
                    current_day = valid_days[0]
                    for pos in range(1, len(valid_indices)):
                        if valid_days[pos] != current_day:
                            day_groups.append((str(current_day), valid_indices[start:pos]))
                            start = pos
                            current_day = valid_days[pos]
                    day_groups.append((str(current_day), valid_indices[start:]))

            for day, idxs in day_groups:
                current: list[list[float]] = []
                prev_lat = prev_lon = None
                day_segment_idx = 0
                ordered_idxs = idxs
                if len(ordered_idxs) > max_line_points:
                    ordered_idxs = ordered_idxs[_sample_indices(len(ordered_idxs), max_line_points)]
                coord_df = self.state.df.loc[ordered_idxs, [self.state.lon_col, self.state.lat_col]]
                lon_values = coord_df[self.state.lon_col].to_numpy()
                lat_values = coord_df[self.state.lat_col].to_numpy()
                for pos in range(len(ordered_idxs)):
                    lon = lon_values[pos]
                    lat = lat_values[pos]
                    if pd.isna(lon) or pd.isna(lat):
                        continue
                    lat_f = float(lat)
                    lon_f = float(lon)
                    if prev_lat is not None:
                        gap = _haversine_m(prev_lat, prev_lon, lat_f, lon_f)
                        if gap > MAX_SEGMENT_DISTANCE_METERS:
                            if len(current) >= 2:
                                segments.append(
                                    {
                                        "seg_id": f"{day}:{day_segment_idx}",
                                        "day": str(day),
                                        "coords": current,
                                    }
                                )
                                day_segment_idx += 1
                            current = []
                    current.append([lat_f, lon_f])
                    prev_lat, prev_lon = lat_f, lon_f
                if len(current) >= 2:
                    segments.append(
                        {
                            "seg_id": f"{day}:{day_segment_idx}",
                            "day": str(day),
                            "coords": current,
                        }
                    )

            self.state._cached_route_key = cache_key
            self.state._cached_route_segments = segments

        selected_day = self.state.selected_day
        return [
            {"day": seg["day"], "coords": seg["coords"], "selected": seg["day"] == selected_day}
            for seg in self.state._cached_route_segments
        ]

    def _update_modified_flags(self, indices: set[int]) -> None:
        for idx in indices:
            if self._is_modified(idx):
                self.state.modified_idx.add(idx)
            else:
                self.state.modified_idx.discard(idx)

    def _persist_origin_db(self) -> None:
        self.state.db_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.state.origin_db_path) as conn:
            self.state.origin_df.to_sql("origin_records", conn, if_exists="replace", index_label="row_idx")

    def _persist_edited_db(self) -> None:
        self.state.db_dir.mkdir(parents=True, exist_ok=True)
        if not self.state.modified_idx:
            with sqlite3.connect(self.state.edited_db_path) as conn:
                conn.execute("DROP TABLE IF EXISTS edited_records")
            return

        idxs = sorted(self.state.modified_idx)
        edited_df = self.state.origin_df.loc[idxs].copy()
        edited_df.insert(0, "source_row_idx", idxs)
        edited_df["original_lon"] = self.state.origin_df.loc[idxs, self.state.lon_col].to_numpy()
        edited_df["original_lat"] = self.state.origin_df.loc[idxs, self.state.lat_col].to_numpy()
        edited_df["edited_lon"] = self.state.df.loc[idxs, self.state.lon_col].to_numpy()
        edited_df["edited_lat"] = self.state.df.loc[idxs, self.state.lat_col].to_numpy()
        edited_df["manual_note"] = self.state.df.loc[idxs, "manual_note"].to_numpy()
        edited_df["manual_timezone"] = self.state.df.loc[idxs, "manual_timezone"].to_numpy()
        edited_df["manual_mode"] = self.state.df.loc[idxs, "manual_mode"].to_numpy()
        edited_df["manual_updated_at"] = self.state.df.loc[idxs, "manual_updated_at"].to_numpy()
        with sqlite3.connect(self.state.edited_db_path) as conn:
            edited_df.to_sql("edited_records", conn, if_exists="replace", index=False)

    def _build_output_df(self) -> pd.DataFrame:
        output_df = self.state.origin_df.copy()
        for col in ["manual_note", "manual_timezone", "manual_mode", "manual_updated_at"]:
            if col not in output_df.columns:
                output_df[col] = self.state.df[col]
            else:
                output_df[col] = self.state.df[col].to_numpy()
        if self.state.modified_idx:
            idxs = sorted(self.state.modified_idx)
            output_df.loc[idxs, self.state.lon_col] = self.state.df.loc[idxs, self.state.lon_col].to_numpy()
            output_df.loc[idxs, self.state.lat_col] = self.state.df.loc[idxs, self.state.lat_col].to_numpy()
        return output_df

    def _persist_output_db(self, output_df: pd.DataFrame) -> None:
        self.state.db_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.state.output_db_path) as conn:
            output_df.to_sql("output_records", conn, if_exists="replace", index_label="row_idx")

    def _export_csv(self, path: Path) -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        output_df = self._build_output_df()
        output_df.to_csv(path, index=False)
        self._persist_output_db(output_df)
        self.state.export_revision = self.state.edit_revision
        return {"ok": True, "path": str(path), "output_db": str(self.state.output_db_path)}

    @pyqtSlot(result="QVariant")
    def requestMeta(self) -> dict[str, Any]:
        df_view = self.state.get_view_df()
        south = west = north = east = None
        center_lat = center_lon = None
        if not df_view.empty:
            valid = df_view[[self.state.lat_col, self.state.lon_col]].dropna()
            if not valid.empty:
                south = float(valid[self.state.lat_col].min())
                west = float(valid[self.state.lon_col].min())
                north = float(valid[self.state.lat_col].max())
                east = float(valid[self.state.lon_col].max())
                center_lat = float(valid[self.state.lat_col].mean())
                center_lon = float(valid[self.state.lon_col].mean())
        elif self.state.data_bounds is not None:
            south, west, north, east = self.state.data_bounds
        bounds_payload = None
        if None not in {south, west, north, east}:
            bounds_payload = {"south": south, "west": west, "north": north, "east": east}
        return {
            "center_lat": center_lat,
            "center_lon": center_lon,
            "data_bounds": bounds_payload,
            "selection_mode": self.state.selection_mode,
            "timezone_name": self.state.timezone_name,
            "status_text": self._status_text(),
            "focus_active": self.state.focus_active,
        }

    def _payload_for_df(self, df_view: pd.DataFrame, zoom: float) -> dict[str, Any]:
        center_lat = center_lon = None
        if not df_view.empty:
            valid = df_view[[self.state.lat_col, self.state.lon_col]].dropna()
            if not valid.empty:
                center_lat = float(valid[self.state.lat_col].mean())
                center_lon = float(valid[self.state.lon_col].mean())
        max_points, max_line_points = self._viewport_limits(zoom)
        return {
            "route_segments": self._route_segments(df_view, max_line_points),
            "point_features": self._serialize_points(df_view, max_points),
            "center_lat": center_lat,
            "center_lon": center_lon,
            "status_text": self._status_text(),
            "selection_mode": self.state.selection_mode,
            "timezone_name": self.state.timezone_name,
            "focus_active": self.state.focus_active,
        }

    @pyqtSlot(result="QVariant")
    def requestData(self) -> dict[str, Any]:
        return self._payload_for_df(self.state.get_view_df(), zoom=12.0)

    @pyqtSlot(float, float, float, float, float, result="QVariant")
    def requestDataForViewport(
        self, south: float, west: float, north: float, east: float, zoom: float
    ) -> dict[str, Any]:
        viewport_mask = self._viewport_mask(south, west, north, east)
        df_base = self.state.get_view_df()
        df_view = df_base[viewport_mask]
        return self._payload_for_df(df_view, zoom=zoom)

    @pyqtSlot(str, result="QVariant")
    def setMode(self, mode: str) -> dict[str, Any]:
        if mode not in {"line", "point"}:
            return {"ok": False, "error": f"invalid mode: {mode}"}
        self.state.selection_mode = mode
        self.state.selected_idx.clear()
        self.state.selected_day = None
        return {"ok": True}

    @pyqtSlot(str, result="QVariant")
    def setTimezone(self, timezone_name: str) -> dict[str, Any]:
        try:
            ZoneInfo(timezone_name)
        except Exception:
            return {"ok": False, "error": f"invalid timezone: {timezone_name}"}
        self.state.timezone_name = timezone_name
        self.state.invalidate_time_cache()
        if self.state.selected_day is not None and self.state.dt_series is not None:
            if not self._indices_for_day(self.state.selected_day):
                self.state.selected_day = None
        return {"ok": True}

    @pyqtSlot(result="QVariant")
    def enterFocus(self) -> dict[str, Any]:
        if self.state.focus_active:
            return {"ok": False, "error": "focus mode already active"}
        active_selection = self._active_selection()
        if not active_selection:
            return {"ok": False, "error": "no active selection to focus"}
        self.state.focus_active = True
        self.state.focus_history_depth = len(self.state.history)
        self.state.focus_selected_idx = set(self.state.selected_idx)
        self.state.focus_selected_day = self.state.selected_day
        self.state.update_view_indices(active_selection)
        return {"ok": True, "focused": len(active_selection)}

    @pyqtSlot(result="QVariant")
    def saveFocus(self) -> dict[str, Any]:
        if not self.state.focus_active:
            return {"ok": False, "error": "focus mode is not active"}
        self.state.focus_active = False
        self.state.focus_history_depth = 0
        self.state.focus_selected_idx = set()
        self.state.focus_selected_day = None
        self.state.update_view_all()
        return {"ok": True}

    @pyqtSlot(result="QVariant")
    def cancelFocus(self) -> dict[str, Any]:
        if not self.state.focus_active:
            return {"ok": False, "error": "focus mode is not active"}
        while len(self.state.history) > self.state.focus_history_depth:
            self._undo_last_batch()
        self.state.focus_active = False
        self.state.focus_history_depth = 0
        self.state.selected_idx = set(self.state.focus_selected_idx)
        self.state.selected_day = self.state.focus_selected_day
        self.state.focus_selected_idx = set()
        self.state.focus_selected_day = None
        self.state.update_view_all()
        return {"ok": True}

    @pyqtSlot(str, result="QVariant")
    def selectDay(self, day: str) -> dict[str, Any]:
        if self.state.selection_mode != "line":
            return {"ok": False, "error": "day selection is only available in line mode"}
        if not day:
            return {"ok": False, "error": "empty day selection"}
        idxs = self._indices_for_day(day)
        if not idxs:
            return {"ok": False, "error": f"no points found for day {day}"}
        self.state.selected_day = day
        self.state.selected_idx = idxs
        return {"ok": True, "selected": len(idxs)}

    @pyqtSlot(float, float, float, float, result="QVariant")
    def selectBounds(self, south: float, west: float, north: float, east: float) -> dict[str, Any]:
        if self.state.selection_mode != "point":
            return {"ok": False, "error": "rectangle selection is only available in point mode"}
        df_view = self.state.get_view_df()
        mask = (
            (df_view[self.state.lon_col] >= west)
            & (df_view[self.state.lon_col] <= east)
            & (df_view[self.state.lat_col] >= south)
            & (df_view[self.state.lat_col] <= north)
        )
        self.state.selected_idx = set(df_view[mask].index.tolist())
        self.state.selected_day = None
        return {"ok": True, "selected": len(self.state.selected_idx)}

    @pyqtSlot(int, bool, result="QVariant")
    def togglePointSelection(self, idx: int, additive: bool) -> dict[str, Any]:
        if self.state.selection_mode != "point":
            return {"ok": False, "error": "point selection is only available in point mode"}
        if idx not in self.state.df.index:
            return {"ok": False, "error": f"invalid index: {idx}"}
        if not additive:
            self.state.selected_idx = {idx}
        elif idx in self.state.selected_idx:
            self.state.selected_idx.discard(idx)
        else:
            self.state.selected_idx.add(idx)
        self.state.selected_day = None
        return {"ok": True, "selected": len(self.state.selected_idx)}

    @pyqtSlot("QVariantList", bool, result="QVariant")
    def selectIndices(self, indices: list[Any], additive: bool) -> dict[str, Any]:
        if self.state.selection_mode != "point":
            return {"ok": False, "error": "point selection is only available in point mode"}
        valid_indices = {
            int(idx) for idx in indices if isinstance(idx, (int, float)) and int(idx) in self.state.df.index
        }
        if additive:
            self.state.selected_idx.update(valid_indices)
        else:
            self.state.selected_idx = valid_indices
        self.state.selected_day = None
        return {"ok": True, "selected": len(self.state.selected_idx)}

    @pyqtSlot(result="QVariant")
    def clearSelection(self) -> dict[str, Any]:
        self.state.selected_idx.clear()
        self.state.selected_day = None
        return {"ok": True}

    @pyqtSlot(float, float, result="QVariant")
    def applyWindow(self, before_hours: float, after_hours: float) -> dict[str, Any]:
        # Compatibility slot kept intentionally. New workflow always renders the full dataset.
        _ = before_hours
        _ = after_hours
        self.state.update_view_all()
        return {"ok": True}

    @pyqtSlot(result="QVariant")
    def showAll(self) -> dict[str, Any]:
        self.state.update_view_all()
        return {"ok": True}

    @pyqtSlot(str, result="QVariant")
    def convert(self, direction: str) -> dict[str, Any]:
        if direction not in {"gcj2wgs", "wgs2gcj"}:
            return {"ok": False, "error": f"invalid direction: {direction}"}
        active_selection = self._active_selection()
        if not active_selection:
            return {"ok": False, "error": "no active selection"}

        history_batch: list[dict[str, Any]] = []
        now_tag = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for idx in sorted(active_selection):
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
                    "timezone": self.state.df.at[idx, "manual_timezone"],
                    "mode": self.state.df.at[idx, "manual_mode"],
                    "updated_at": self.state.df.at[idx, "manual_updated_at"],
                }
            )
            if direction == "gcj2wgs":
                new_lon, new_lat = gcj02_to_wgs84(float(lon), float(lat))
                note = f"manual:GCJ->WGS@{now_tag}"
            else:
                new_lon, new_lat = wgs84_to_gcj02(float(lon), float(lat))
                note = f"manual:WGS->GCJ@{now_tag}"
            prev_note = self.state.df.at[idx, "manual_note"]
            self.state.df.at[idx, self.state.lon_col] = new_lon
            self.state.df.at[idx, self.state.lat_col] = new_lat
            self.state.df.at[idx, "manual_note"] = f"{prev_note} | {note}" if prev_note else note
            self.state.df.at[idx, "manual_timezone"] = self.state.timezone_name
            self.state.df.at[idx, "manual_mode"] = self.state.selection_mode
            self.state.df.at[idx, "manual_updated_at"] = now_tag

        if not history_batch:
            return {"ok": False, "error": "no valid coordinates in selection"}
        self.state.history.append(history_batch)
        self._update_modified_flags(active_selection)
        self.state.invalidate_geometry_cache()
        self._persist_edited_db()
        self._mark_edited()
        return {"ok": True, "converted": len(history_batch)}

    @pyqtSlot(result="QVariant")
    def autoSmoothSelection(self) -> dict[str, Any]:
        active_selection = sorted(self._active_selection())
        if len(active_selection) < 2:
            return {"ok": False, "error": "need at least 2 selected points for auto repair"}

        lon_series = self.state.df.loc[active_selection, self.state.lon_col]
        lat_series = self.state.df.loc[active_selection, self.state.lat_col]
        if lon_series.isna().any() or lat_series.isna().any():
            return {"ok": False, "error": "selection contains invalid coordinates"}

        fixed_lons = [float(lon_series.iloc[0])]
        fixed_lats = [float(lat_series.iloc[0])]
        prev_valid_lon = None
        prev_valid_lat = None
        last_valid_lon = fixed_lons[0]
        last_valid_lat = fixed_lats[0]

        optimized = np.column_stack([lon_series.to_numpy(dtype=float), lat_series.to_numpy(dtype=float)])

        for pos in range(1, len(active_selection)):
            curr_raw_lon = float(lon_series.iloc[pos])
            curr_raw_lat = float(lat_series.iloc[pos])
            curr_fix_lon, curr_fix_lat = gcj02_to_wgs84(curr_raw_lon, curr_raw_lat)

            dist_if_original = self._geo_distance_m(
                last_valid_lon, last_valid_lat, curr_raw_lon, curr_raw_lat
            )
            dist_if_fixed = self._geo_distance_m(
                last_valid_lon, last_valid_lat, curr_fix_lon, curr_fix_lat
            )
            improvement = dist_if_original - dist_if_fixed

            cond_jump = dist_if_original > AUTO_REPAIR_JUMP_DETECT_THRESHOLD
            cond_smooth = dist_if_fixed < AUTO_REPAIR_SMOOTH_THRESHOLD
            sharp_turn = False
            required_improvement = AUTO_REPAIR_MIN_IMPROVEMENT

            angle_prev_raw = None
            angle_prev_fix = None
            angle_next_raw = None
            angle_next_fix = None

            if prev_valid_lon is not None:
                angle_prev_raw = self._turning_angle(
                    (prev_valid_lon, prev_valid_lat),
                    (last_valid_lon, last_valid_lat),
                    (curr_raw_lon, curr_raw_lat),
                )
                angle_prev_fix = self._turning_angle(
                    (prev_valid_lon, prev_valid_lat),
                    (last_valid_lon, last_valid_lat),
                    (curr_fix_lon, curr_fix_lat),
                )

            if pos + 2 < len(active_selection):
                next_raw_lon = float(lon_series.iloc[pos + 1])
                next_raw_lat = float(lat_series.iloc[pos + 1])
                next_next_raw_lon = float(lon_series.iloc[pos + 2])
                next_next_raw_lat = float(lat_series.iloc[pos + 2])
                angle_next_raw = self._turning_angle(
                    (curr_raw_lon, curr_raw_lat),
                    (next_raw_lon, next_raw_lat),
                    (next_next_raw_lon, next_next_raw_lat),
                )
                angle_next_fix = self._turning_angle(
                    (curr_fix_lon, curr_fix_lat),
                    (next_raw_lon, next_raw_lat),
                    (next_next_raw_lon, next_next_raw_lat),
                )

            angle_margin = 20.0
            if angle_prev_fix is not None and angle_prev_fix < AUTO_REPAIR_SHARP_TURN_DEG:
                sharp_turn = True
            if angle_next_fix is not None and angle_next_fix < AUTO_REPAIR_SHARP_TURN_DEG:
                sharp_turn = True
            if angle_prev_fix is not None and angle_prev_raw is not None:
                if angle_prev_fix + angle_margin < angle_prev_raw:
                    sharp_turn = True
            if angle_next_fix is not None and angle_next_raw is not None:
                if angle_next_fix + angle_margin < angle_next_raw:
                    sharp_turn = True
            if sharp_turn:
                required_improvement = AUTO_REPAIR_MIN_IMPROVEMENT * AUTO_REPAIR_SHARP_GAIN_MULTIPLIER

            final_lon = curr_raw_lon
            final_lat = curr_raw_lat

            if cond_jump and cond_smooth:
                if improvement >= required_improvement:
                    final_lon, final_lat = curr_fix_lon, curr_fix_lat
            elif abs(improvement) < AUTO_REPAIR_AMBIGUOUS_THRESHOLD and pos + 1 < len(active_selection):
                next_raw_lon = float(lon_series.iloc[pos + 1])
                next_raw_lat = float(lat_series.iloc[pos + 1])
                cost_raw = self._geo_distance_m(
                    last_valid_lon, last_valid_lat, curr_raw_lon, curr_raw_lat
                ) + self._geo_distance_m(curr_raw_lon, curr_raw_lat, next_raw_lon, next_raw_lat)
                cost_fix = self._geo_distance_m(
                    last_valid_lon, last_valid_lat, curr_fix_lon, curr_fix_lat
                ) + self._geo_distance_m(curr_fix_lon, curr_fix_lat, next_raw_lon, next_raw_lat)
                lookahead_threshold = AUTO_REPAIR_LOOKAHEAD_GAIN
                if sharp_turn:
                    lookahead_threshold *= AUTO_REPAIR_SHARP_GAIN_MULTIPLIER
                if cost_fix + lookahead_threshold < cost_raw:
                    final_lon, final_lat = curr_fix_lon, curr_fix_lat

            optimized[pos, 0] = final_lon
            optimized[pos, 1] = final_lat
            fixed_lons.append(final_lon)
            fixed_lats.append(final_lat)
            prev_valid_lon = last_valid_lon
            prev_valid_lat = last_valid_lat
            last_valid_lon = final_lon
            last_valid_lat = final_lat

        changed_positions = {
            pos
            for pos in range(len(active_selection))
            if (
                abs(float(optimized[pos, 0]) - float(lon_series.iloc[pos])) > MODIFIED_EPSILON
                or abs(float(optimized[pos, 1]) - float(lat_series.iloc[pos])) > MODIFIED_EPSILON
            )
        }
        if not changed_positions:
            return {"ok": False, "error": "auto repair produced no editable changes"}

        now_tag = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        history_batch: list[dict[str, Any]] = []
        for pos in sorted(changed_positions):
            idx = active_selection[pos]
            old_lon = self.state.df.at[idx, self.state.lon_col]
            old_lat = self.state.df.at[idx, self.state.lat_col]
            new_lon = float(optimized[pos, 0])
            new_lat = float(optimized[pos, 1])
            if (
                abs(float(old_lon) - new_lon) <= MODIFIED_EPSILON
                and abs(float(old_lat) - new_lat) <= MODIFIED_EPSILON
            ):
                continue
            history_batch.append(
                {
                    "idx": idx,
                    "lon": old_lon,
                    "lat": old_lat,
                    "note": self.state.df.at[idx, "manual_note"],
                    "timezone": self.state.df.at[idx, "manual_timezone"],
                    "mode": self.state.df.at[idx, "manual_mode"],
                    "updated_at": self.state.df.at[idx, "manual_updated_at"],
                }
            )
            prev_note = self.state.df.at[idx, "manual_note"]
            note = f"manual:AUTOCOORD_RUNPY@{now_tag}"
            self.state.df.at[idx, self.state.lon_col] = new_lon
            self.state.df.at[idx, self.state.lat_col] = new_lat
            self.state.df.at[idx, "manual_note"] = f"{prev_note} | {note}" if prev_note else note
            self.state.df.at[idx, "manual_timezone"] = self.state.timezone_name
            self.state.df.at[idx, "manual_mode"] = f"{self.state.selection_mode}:autocoord_runpy"
            self.state.df.at[idx, "manual_updated_at"] = now_tag

        if not history_batch:
            return {"ok": False, "error": "auto smooth produced no editable changes"}
        changed_indices = {item["idx"] for item in history_batch}
        self.state.history.append(history_batch)
        self._update_modified_flags(changed_indices)
        self.state.invalidate_geometry_cache()
        self._persist_edited_db()
        self._mark_edited()
        return {"ok": True, "smoothed": len(history_batch)}

    @pyqtSlot(result="QVariant")
    def undo(self) -> dict[str, Any]:
        return self._undo_last_batch()

    @pyqtSlot(result="QVariant")
    def save(self) -> dict[str, Any]:
        path = self.state.output_path or DEFAULT_OUTPUT_PATH
        return self._export_csv(path)

    @pyqtSlot(result="QVariant")
    def saveAs(self) -> dict[str, Any]:
        file_path, _ = QFileDialog.getSaveFileName(
            None,
            "Export CSV",
            str((self.state.output_path or DEFAULT_OUTPUT_PATH).resolve()),
            "CSV Files (*.csv)",
        )
        if not file_path:
            return {"ok": False}
        export_path = Path(file_path)
        self.state.output_path = export_path
        return self._export_csv(export_path)


class ManualEditorView(QWebEngineView):
    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend

    def closeEvent(self, event: QCloseEvent) -> None:
        state = self.backend.state
        if state.edit_revision != state.export_revision:
            result = QMessageBox.question(
                self,
                "Unexported Changes",
                "Current edits have not been exported to CSV. Exit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)


def load_state(input_path: Path, output_path: Optional[Path], db_dir: Optional[Path]) -> AppState:
    df = pd.read_csv(input_path, low_memory=False).reset_index(drop=True)
    origin_df = df.copy()

    if "clean_longitude" in df.columns and "clean_latitude" in df.columns:
        lon_col, lat_col = "clean_longitude", "clean_latitude"
    elif "longitude" in df.columns and "latitude" in df.columns:
        lon_col, lat_col = "longitude", "latitude"
    else:
        raise SystemExit("Missing longitude/latitude columns.")

    time_col = "geoTime" if "geoTime" in df.columns else None
    dt_series = _normalize_time_series(df[time_col]) if time_col else None

    df["_orig_lon"] = origin_df[lon_col]
    df["_orig_lat"] = origin_df[lat_col]
    if "manual_note" not in df.columns:
        df["manual_note"] = ""
    if "manual_timezone" not in df.columns:
        df["manual_timezone"] = ""
    if "manual_mode" not in df.columns:
        df["manual_mode"] = ""
    if "manual_updated_at" not in df.columns:
        df["manual_updated_at"] = ""

    valid_coords = df[[lat_col, lon_col]].dropna()
    data_bounds = None
    if not valid_coords.empty:
        data_bounds = (
            float(valid_coords[lat_col].min()),
            float(valid_coords[lon_col].min()),
            float(valid_coords[lat_col].max()),
            float(valid_coords[lon_col].max()),
        )

    stem = input_path.stem
    store_dir = db_dir or Path("data/manual_store") / stem
    state = AppState(
        df=df,
        origin_df=origin_df,
        lon_col=lon_col,
        lat_col=lat_col,
        time_col=time_col,
        dt_series=dt_series,
        input_path=input_path,
        output_path=output_path,
        db_dir=store_dir,
        origin_db_path=store_dir / "origin.db",
        edited_db_path=store_dir / "edited.db",
        output_db_path=store_dir / "output.db",
        data_bounds=data_bounds,
    )
    state.update_view_all()

    backend_seed = Backend(state)
    backend_seed._persist_origin_db()
    backend_seed._persist_edited_db()
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual GPS repair GUI")
    parser.add_argument("--input", "-i", type=str, help="input CSV")
    parser.add_argument("--output", "-o", type=str, help="exported CSV path")
    parser.add_argument("--db-dir", type=str, help="directory for origin/edited/output SQLite files")
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
    db_dir = Path(args.db_dir) if args.db_dir else None

    state = load_state(input_path, output_path, db_dir)
    backend = Backend(state)

    view = ManualEditorView(backend)
    channel = QWebChannel()
    channel.registerObject("backend", backend)
    view.page().setWebChannel(channel)
    view.setHtml(HTML)
    view.setWindowTitle("Manual GPS Repair")
    view.resize(1360, 860)
    view.show()
    app.exec()


if __name__ == "__main__":
    main()
