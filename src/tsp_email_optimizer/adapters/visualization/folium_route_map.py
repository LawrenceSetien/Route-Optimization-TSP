from __future__ import annotations

import logging
from pathlib import Path

import folium
import requests

from tsp_email_optimizer.domain.models import GeocodedStop, OptimizedRoute

logger = logging.getLogger(__name__)


class FoliumRouteMapRenderer:
    def __init__(
        self,
        output_dir: str,
        api_key: str,
        profile: str = "driving-car",
        timeout_s: int = 30,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._profile = profile
        self._timeout_s = timeout_s
        self._headers = {"Authorization": api_key, "Content-Type": "application/json"}

    def render(self, route: OptimizedRoute) -> str | None:
        points = self._build_points(route)
        if not points:
            logger.warning("Skipping map render request_id=%s no points available", route.request_id)
            return None

        map_center = [points[0].lat, points[0].lon]
        fmap = folium.Map(location=map_center, zoom_start=13, tiles="OpenStreetMap")

        if route.start_location is not None:
            folium.Marker(
                location=[route.start_location.lat, route.start_location.lon],
                popup=f"Inicio: {route.start_location.address}",
                tooltip="Inicio",
                icon=folium.Icon(color="green", icon="play"),
            ).add_to(fmap)

        for optimized_index, stop in enumerate(route.ordered_stops, start=1):
            popup = (
                f"Parada {optimized_index}<br>"
                f"Direccion: {stop.address}<br>"
                f"Indice original: {stop.original_index}"
            )
            folium.Marker(
                location=[stop.lat, stop.lon],
                popup=popup,
                tooltip=f"{optimized_index}. {stop.address}",
                icon=folium.DivIcon(
                    html=(
                        f"<div style='"
                        "width: 26px; height: 26px; border-radius: 50%; "
                        "background: #0d6efd; color: white; border: 2px solid white; "
                        "box-shadow: 0 0 4px rgba(0, 0, 0, 0.35); "
                        "text-align: center; line-height: 22px; font-size: 12px; "
                        "font-weight: 700;'>"
                        f"{optimized_index}</div>"
                    )
                ),
            ).add_to(fmap)

        polyline_points = self._get_driving_geometry(points)
        if not polyline_points:
            polyline_points = [[point.lat, point.lon] for point in points]
            logger.warning(
                "Using straight-line fallback for map request_id=%s points=%d",
                route.request_id,
                len(points),
            )
        if len(polyline_points) >= 2:
            folium.PolyLine(polyline_points, color="#198754", weight=4, opacity=0.9).add_to(fmap)

        min_lat = min(point.lat for point in points)
        max_lat = max(point.lat for point in points)
        min_lon = min(point.lon for point in points)
        max_lon = max(point.lon for point in points)
        fmap.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

        output_path = self._output_dir / f"{route.request_id}.html"
        fmap.save(str(output_path))
        logger.info(
            "Map generated request_id=%s points=%d output=%r",
            route.request_id,
            len(points),
            str(output_path),
        )
        return str(output_path)

    @staticmethod
    def _build_points(route: OptimizedRoute) -> list[GeocodedStop]:
        points: list[GeocodedStop] = []
        if route.start_location is not None:
            points.append(route.start_location)
        points.extend(route.ordered_stops)
        return points

    def _get_driving_geometry(self, points: list[GeocodedStop]) -> list[list[float]]:
        if len(points) < 2:
            return []
        coordinates = [[point.lon, point.lat] for point in points]
        # The optimizer uses a depot start/end when available; mirror it in map geometry.
        if len(coordinates) >= 2 and points[0] == points[-1]:
            pass
        elif len(coordinates) >= 2 and len(points) > 1 and points[0].original_index == 0:
            coordinates.append(coordinates[0])

        url = f"https://api.openrouteservice.org/v2/directions/{self._profile}/geojson"
        payload = {
            "coordinates": coordinates,
            "instructions": False,
            "maneuvers": False,
        }
        response = requests.post(
            url,
            headers=self._headers,
            json=payload,
            timeout=self._timeout_s,
        )
        if response.status_code >= 400:
            logger.error("ORS directions geometry failed status=%d body=%s", response.status_code, response.text)
            return []
        data = response.json()
        features = data.get("features", [])
        if not features:
            return []
        geometry = features[0].get("geometry", {})
        coords = geometry.get("coordinates", [])
        if not coords:
            return []
        # ORS returns [lon, lat], Folium expects [lat, lon].
        return [[float(coord[1]), float(coord[0])] for coord in coords if len(coord) >= 2]


