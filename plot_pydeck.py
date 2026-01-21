"""
Render GPS trajectories with pydeck (WebGL) for large datasets.
Output: interactive HTML file.
"""

from pathlib import Path

import pandas as pd

try:
    import pydeck as pdk
except ImportError as exc:
    raise SystemExit(
        "pydeck is not installed. Run: pip install pydeck"
    ) from exc


# ================= Config =================
INPUT_FILE = "./output/gps_data_perfect.csv"
OUTPUT_HTML = "trajectory_pydeck.html"
DRAW_RAW_PATH = True
DRAW_CLEAN_PATH = True
DRAW_POINTS = False

# Map style: external free basemap (no token required).
MAP_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
# =========================================


def visualize_pydeck(file_path: str, output_html: str) -> None:
    df = pd.read_csv(file_path, low_memory=False)

    has_clean = "clean_latitude" in df.columns and "clean_longitude" in df.columns

    df_raw = df.dropna(subset=["latitude", "longitude"])
    df_clean = df.dropna(subset=["clean_latitude", "clean_longitude"]) if has_clean else pd.DataFrame()

    if df_raw.empty and df_clean.empty:
        raise ValueError("No valid coordinates to plot.")

    if not df_clean.empty:
        center_lat = df_clean.iloc[0]["clean_latitude"]
        center_lon = df_clean.iloc[0]["clean_longitude"]
    else:
        center_lat = df_raw.iloc[0]["latitude"]
        center_lon = df_raw.iloc[0]["longitude"]

    layers = []

    if DRAW_RAW_PATH and not df_raw.empty:
        raw_coords = df_raw[["longitude", "latitude"]].values.tolist()
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=[{"path": raw_coords}],
                get_path="path",
                get_width=3,
                get_color=[220, 20, 60],
                width_min_pixels=2,
                pickable=False,
            )
        )

    if DRAW_CLEAN_PATH and not df_clean.empty:
        clean_coords = df_clean[["clean_longitude", "clean_latitude"]].values.tolist()
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=[{"path": clean_coords}],
                get_path="path",
                get_width=3,
                get_color=[0, 120, 255],
                width_min_pixels=2,
                pickable=False,
            )
        )

    if DRAW_POINTS and not df_clean.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=df_clean,
                get_position="[clean_longitude, clean_latitude]",
                get_radius=6,
                get_fill_color=[0, 120, 255],
                pickable=False,
            )
        )

    view_state = pdk.ViewState(
        latitude=center_lat,
        longitude=center_lon,
        zoom=12,
        pitch=0,
    )

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_style=MAP_STYLE,
        tooltip=None,
        controller={"doubleClickZoom": False},
    )

    deck.to_html(output_html, title="GPS Trajectory (pydeck)")
    print(f"Wrote {output_html}")


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    visualize_pydeck(
        str(base_dir / INPUT_FILE),
        str(base_dir / OUTPUT_HTML),
    )
