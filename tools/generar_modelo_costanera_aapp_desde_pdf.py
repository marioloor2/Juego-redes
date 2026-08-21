"""Genera el modelo vectorial Costanera_Acacias_AAPP desde el plano PDF fuente."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re

import numpy as np
import pdfplumber
from pypdf import PdfReader


RED = (1.0, 0.0, 0.0)
BLUE = (0.0, 0.0, 1.0)
YELLOW = (1.0, 1.0, 0.0)
GREEN = (0.0, 1.0, 0.0)
CONTROLS = {
    "P1": (603148.5826, 9754589.0519),
    "P2": (603657.8214, 9754589.0519),
    "P3": (603657.8214, 9755145.2453),
    "P4": (603148.5826, 9755145.2453),
    "P5": (603499.9308, 9754845.9241),
    "P6": (603551.9310, 9754895.6160),
    "P7": (603610.7095, 9754841.6015),
    "P8": (603534.8084, 9754810.6874),
}
UNITS_PER_METER = 3.2
DISPLAY_MARGIN_METERS = 24.0
# Radio para dar por unidos dos extremos de tubería, en puntos del PDF.
ENDPOINT_SNAP_POINTS = 2.0
# Por debajo de esta longitud el trozo no merece ser un tramo propio.
SHORT_RUN_METERS = 5.0
# Un vecino continúa el trazo si el quiebre en el accesorio no pasa de esto.
MAX_MERGE_DEVIATION_DEGREES = 45.0
# Aparatos que representan por sí solos el final de la tubería.
DEVICE_TYPES = {"valve", "valveBox", "hydrant", "macroMeter", "regulator", "airValve", "drainValve"}
# Un extremo a menos de esta distancia de un aparato es su conexión, no un tapón.
CAP_TO_DEVICE_UNITS = 16.0


def color_tuple(value):
    return tuple(round(float(component), 4) for component in (value or ()))


def affine_fit(source: np.ndarray, target: np.ndarray):
    design = np.column_stack((source, np.ones(len(source))))
    transform, *_ = np.linalg.lstsq(design, target, rcond=None)
    residual = design @ transform - target
    rms = math.sqrt(float(np.mean(np.sum(residual * residual, axis=1))))
    return transform, rms


def apply_affine(point, transform):
    return np.append(np.asarray(point, dtype=float), 1.0) @ transform


WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
UTM_K0 = 0.9996
UTM_ZONE = 17
SATELLITE_ZOOM = 19


def utm_to_latlon(east, north):
    """Transversa de Mercator inversa (serie de Krüger), zona 17 sur."""
    x = east - 500000.0
    y = north - 10000000.0
    e1 = (1 - math.sqrt(1 - WGS84_E2)) / (1 + math.sqrt(1 - WGS84_E2))
    mu = (y / UTM_K0) / (
        WGS84_A * (1 - WGS84_E2 / 4 - 3 * WGS84_E2**2 / 64 - 5 * WGS84_E2**3 / 256)
    )
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
            + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
            + (151 * e1**3 / 96) * math.sin(6 * mu)
            + (1097 * e1**4 / 512) * math.sin(8 * mu))
    ep2 = WGS84_E2 / (1 - WGS84_E2)
    c1 = ep2 * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(phi1) ** 2)
    r1 = WGS84_A * (1 - WGS84_E2) / (1 - WGS84_E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * UTM_K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720
    )
    lon = math.radians(UTM_ZONE * 6 - 183) + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120
    ) / math.cos(phi1)
    return math.degrees(lat), math.degrees(lon)


def latlon_to_tile_pixel(lat, lon, zoom=SATELLITE_ZOOM):
    """Web Mercator en píxeles, con mosaicos de 256."""
    world = 256 * 2**zoom
    sine = math.sin(math.radians(lat))
    return (
        (lon + 180.0) / 360.0 * world,
        (0.5 - math.log((1 + sine) / (1 - sine)) / (4 * math.pi)) * world,
    )


def build_georeference(origin_east, origin_north, rms, source_name):
    return {
        "crs": "EPSG:32717",
        "orientation": "north-up",
        "handednessValidated": True,
        "controlSource": source_name,
        "controlPointCount": len(CONTROLS),
        "controlRmsMeters": round(rms, 6),
        "displayOrigin": {
            "east": round(origin_east, 4),
            "north": round(origin_north, 4),
            "unitsPerMeter": UNITS_PER_METER,
        },
    }


def build_satellite_layer(origin_east, origin_north):
    """Afín de píxeles de mosaico a unidades de dibujo, sobre una malla del área."""
    east_span = (max(v[0] for v in CONTROLS.values()) + DISPLAY_MARGIN_METERS) - origin_east
    north_span = origin_north - (min(v[1] for v in CONTROLS.values()) - DISPLAY_MARGIN_METERS)
    steps = 6
    pixels, display = [], []
    for i in range(steps + 1):
        for j in range(steps + 1):
            east = origin_east + east_span * i / steps
            north = origin_north - north_span * j / steps
            pixels.append(latlon_to_tile_pixel(*utm_to_latlon(east, north)))
            display.append((
                (east - origin_east) * UNITS_PER_METER,
                (origin_north - north) * UNITS_PER_METER,
            ))
    tile_origin_x = math.floor(min(p[0] for p in pixels) / 256)
    tile_origin_y = math.floor(min(p[1] for p in pixels) / 256)
    local = np.asarray([
        (px - tile_origin_x * 256, py - tile_origin_y * 256) for px, py in pixels
    ])
    transform, rms = affine_fit(local, np.asarray(display))
    matrix = {
        "a": round(float(transform[0][0]), 10), "b": round(float(transform[0][1]), 10),
        "c": round(float(transform[1][0]), 10), "d": round(float(transform[1][1]), 10),
        "e": round(float(transform[2][0]), 10), "f": round(float(transform[2][1]), 10),
    }
    # Los mosaicos y el SVG comparten ejes; un determinante negativo reflejaría el mapa.
    if matrix["a"] * matrix["d"] - matrix["b"] * matrix["c"] <= 0:
        raise ValueError("La transformación satelital reflejaría el mapa")
    return {
        "zoom": SATELLITE_ZOOM,
        "tileOriginX": tile_origin_x,
        "tileOriginY": tile_origin_y,
        "transform": matrix,
        "rmsDisplayUnits": round(rms, 6),
    }


def line_endpoints(line):
    values = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", line["d"])]
    return np.asarray(values[:2]), np.asarray(values[2:4])


def build_runs(lines):
    """Agrupa las líneas en tramos, absorbiendo los trozos cortos en su vecino.

    Un trozo de menos de SHORT_RUN_METERS no merece ser un tramo propio. Se
    fusiona con el vecino del mismo diámetro que continúe más recto. Nunca se
    cruza un cambio de diámetro ni se toca la tubería de 200 mm, que va fuera de
    contrato. La fusión es de conteo y rotulado: los accesorios intermedios se
    siguen dibujando donde están.
    """
    by_id = {line["id"]: line for line in lines}
    ends = {line["id"]: line_endpoints(line) for line in lines}
    incident = defaultdict(list)
    for line in lines:
        incident[line["from"]].append(line["id"])
        incident[line["to"]].append(line["id"])

    def direction_from(line_id, node_id):
        start, end = ends[line_id]
        near, far = (start, end) if by_id[line_id]["from"] == node_id else (end, start)
        vector = far - near
        return vector / (np.linalg.norm(vector) or 1.0)

    def deviation(line_id, other_id, node_id):
        cosine = float(np.clip(np.dot(direction_from(line_id, node_id),
                                      direction_from(other_id, node_id)), -1, 1))
        return 180.0 - math.degrees(math.acos(cosine))

    parent = {line["id"]: line["id"] for line in lines}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    changed = True
    while changed:
        changed = False
        members = defaultdict(list)
        for line_id in parent:
            members[find(line_id)].append(line_id)
        short_first = sorted(
            (root for root, group in members.items()
             if by_id[root]["diameter"] != "200"
             and sum(by_id[m]["meters"] for m in group) < SHORT_RUN_METERS),
            key=lambda root: sum(by_id[m]["meters"] for m in members[root]),
        )
        for root in short_first:
            group = set(members[root])
            best = None
            for line_id in group:
                for node_id in (by_id[line_id]["from"], by_id[line_id]["to"]):
                    passthrough = len(incident[node_id]) == 2
                    for other_id in incident[node_id]:
                        if other_id in group or find(other_id) == root:
                            continue
                        if by_id[other_id]["diameter"] != by_id[line_id]["diameter"]:
                            continue
                        if by_id[other_id]["diameter"] == "200":
                            continue
                        angle = deviation(line_id, other_id, node_id)
                        if not passthrough and angle > MAX_MERGE_DEVIATION_DEGREES:
                            continue
                        key = (0 if passthrough else 1, angle)
                        if best is None or key < best[0]:
                            best = (key, other_id)
            if best:
                parent[find(best[1])] = root
                changed = True

    members = defaultdict(list)
    for line_id in parent:
        members[find(line_id)].append(line_id)

    runs = []
    for number, (root, group) in enumerate(
            sorted(members.items(), key=lambda item: min(item[1])), 1):
        ordered = order_run(group, by_id)
        total = sum(by_id[m]["meters"] for m in ordered)
        label_line, label_point = run_midpoint(ordered, by_id, ends, total)
        plan = next((by_id[m]["planMeters"] for m in ordered if by_id[m]["planMeters"]), None)
        runs.append({
            "id": f"TR-{number:03d}",
            "diameter": by_id[root]["diameter"],
            "meters": round(total, 2),
            "lines": ordered,
            "labelLineId": label_line,
            "labelPoint": {"x": round(float(label_point[0]), 2),
                           "y": round(float(label_point[1]), 2)},
            "identifier": next((by_id[m]["identifier"] for m in ordered
                                if not by_id[m]["identifier"].startswith("AP-GEO")), by_id[root]["identifier"]),
            "planMeters": plan,
            "excludeFromProgress": by_id[root]["diameter"] == "200",
        })
    return runs


def order_run(group, by_id):
    """Ordena las líneas del tramo de punta a punta."""
    if len(group) == 1:
        return list(group)
    touching = defaultdict(list)
    for line_id in group:
        touching[by_id[line_id]["from"]].append(line_id)
        touching[by_id[line_id]["to"]].append(line_id)
    terminals = [node for node, items in touching.items() if len(items) == 1]
    current_node = terminals[0] if terminals else by_id[sorted(group)[0]]["from"]
    remaining = set(group)
    ordered = []
    while remaining:
        following = next((i for i in touching[current_node] if i in remaining), None)
        if following is None:
            ordered.extend(sorted(remaining))
            break
        ordered.append(following)
        remaining.discard(following)
        line = by_id[following]
        current_node = line["to"] if line["from"] == current_node else line["from"]
    return ordered


def run_midpoint(ordered, by_id, ends, total):
    """Punto medio del tramo completo, no de cada pedacito."""
    walked = 0.0
    half = total / 2.0
    previous_node = None
    for line_id in ordered:
        line = by_id[line_id]
        length = line["meters"]
        if walked + length >= half or line_id == ordered[-1]:
            start, end = ends[line_id]
            if previous_node is not None and line["to"] == previous_node:
                start, end = end, start
            ratio = 0.5 if length <= 0 else min(1.0, max(0.0, (half - walked) / length))
            return line_id, start + (end - start) * ratio
        walked += length
        previous_node = line["to"] if line["from"] != previous_node else line["from"]
    return ordered[0], ends[ordered[0]][0]


def centerline(pair):
    points = np.asarray([point for item in pair for point in item["pts"]], dtype=float)
    center = points.mean(axis=0)
    _, _, vh = np.linalg.svd(points - center, full_matrices=False)
    axis = vh[0]
    projection = (points - center) @ axis
    low = points[projection <= np.quantile(projection, 0.35)].mean(axis=0)
    high = points[projection >= np.quantile(projection, 0.65)].mean(axis=0)
    return low, high


def point_segment_distance(point, start, end):
    point, start, end = map(lambda value: np.asarray(value, dtype=float), (point, start, end))
    vector = end - start
    scale = float(np.dot(vector, vector))
    if scale == 0:
        return float(np.linalg.norm(point - start))
    factor = float(np.clip(np.dot(point - start, vector) / scale, 0, 1))
    return float(np.linalg.norm(point - (start + factor * vector)))


def extract_tramos(source_pdf: Path, page_height: float):
    page = PdfReader(str(source_pdf)).pages[0]
    chunks = []

    def visitor(text, _cm, tm, _font_dict, _font_size):
        value = " ".join(text.replace("\x00", " ").split())
        if value:
            a, b, _c, _d, x, y = tm
            chunks.append({
                "text": value,
                "x": float(x) / 8.0,
                "top": page_height - float(y) / 8.0,
                "angle": math.degrees(math.atan2(b, a)),
            })

    page.extract_text(visitor_text=visitor)
    tramos = []
    for index, item in enumerate(chunks):
        if not item["text"].startswith("Tramo ap-"):
            continue
        combined = " | ".join(value["text"] for value in chunks[index:index + 5])
        identifier = item["text"].replace("Tramo ap-", "").strip()
        diameter_match = re.search(r"D:(\d+)mm", combined)
        length_match = re.search(r"L=([\d.]+)\s*m", combined)
        flow_match = re.search(r"Q=([-\d.]+)\s*L/s", combined)
        tramos.append({
            "identifier": f"ap-{identifier}",
            "diameter": int(diameter_match.group(1)) if diameter_match else None,
            "meters": float(length_match.group(1)) if length_match else None,
            "flow": float(flow_match.group(1)) if flow_match else None,
            "labelPoint": np.array((item["x"], item["top"]), dtype=float),
            "angle": float(item["angle"]),
        })
    return tramos


class DSU:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, value):
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, left, right):
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def box_distance(left, right):
    dx = max(left[0] - right[2], right[0] - left[2], 0)
    dy = max(left[1] - right[3], right[1] - left[3], 0)
    return math.hypot(dx, dy)


def yellow_groups(page):
    objects = []
    for kind in ("lines", "curves"):
        for item in getattr(page, kind):
            if color_tuple(item.get("stroking_color")) != YELLOW:
                continue
            objects.append((
                min(item["x0"], item["x1"]), min(item["top"], item["bottom"]),
                max(item["x0"], item["x1"]), max(item["top"], item["bottom"]),
            ))
    dsu = DSU(len(objects))
    cells = defaultdict(list)
    gap, cell_size = 5.0, 15.0
    for index, box in enumerate(objects):
        left = int(math.floor((box[0] - gap) / cell_size))
        right = int(math.floor((box[2] + gap) / cell_size))
        upper = int(math.floor((box[1] - gap) / cell_size))
        lower = int(math.floor((box[3] + gap) / cell_size))
        candidates = set()
        for gx in range(left, right + 1):
            for gy in range(upper, lower + 1):
                candidates.update(cells[(gx, gy)])
        for other in candidates:
            if box_distance(box, objects[other]) <= gap:
                dsu.union(index, other)
        for gx in range(left, right + 1):
            for gy in range(upper, lower + 1):
                cells[(gx, gy)].append(index)
    members_by_root = defaultdict(list)
    for index in range(len(objects)):
        members_by_root[dsu.find(index)].append(index)
    groups = []
    for members in members_by_root.values():
        if len(members) < 8:
            continue
        box = (
            min(objects[i][0] for i in members), min(objects[i][1] for i in members),
            max(objects[i][2] for i in members), max(objects[i][3] for i in members),
        )
        groups.append({
            "count": len(members),
            "box": box,
            "center": np.array(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)),
        })
    return groups


def build_definition():
    point = lambda canonical, symbol: {
        "geometry": "point", "canonicalName": canonical, "symbol": symbol,
        "identifierRequired": False,
    }
    return {
        "type": "AAPP",
        "name": "Red de agua potable",
        "aliases": ["AP"],
        "operatingPrinciple": "pressurized",
        "structureRules": {
            "loopsAllowed": True,
            "sourceRequired": True,
            "hydraulicNodesAreInventory": False,
        },
        "constructionRules": {
            "startsAt": "macroMeter",
            "direction": "source-to-network",
        },
        "visualizationRules": {
            # La tubería AAPP llega en rollo continuo: no se divide en módulos.
            "pipeSupply": "coil",
            "oneTotalLengthLabelPerLine": True,
            "labelEveryTubeModule": False,
            "tubeDivisionGuidesVisibleOnZoom": False,
            "progressLabels": {"collector": "Tubería 110 mm", "branch": "Tubería 90 mm"},
        },
        "elementRules": {
            "mainPipe": {"geometry": "line", "canonicalName": "Tubería principal", "lengthRequired": True},
            "distributionPipe": {"geometry": "line", "canonicalName": "Tubería de distribución", "lengthRequired": True},
            "externalPipe": {"geometry": "line", "canonicalName": "Tubería exterior", "lengthRequired": False},
            "junction": point("Unión geométrica", "hidden"),
            "cap": point("Tapón", "cap"),
            "elbow90": point("Codo 90°", "elbow90"),
            "elbow135": point("Codo 135°", "elbow135"),
            "semielbow": point("Semicodo", "semielbow"),
            "teeReducer": point("Tee reductora", "teeReducer"),
            "teeEqual": point("Tee igual", "teeEqual"),
            "cross": point("Cruz", "cross"),
            "reducer": point("Reductor", "reducer"),
            "valve": point("Válvula", "valve"),
            "valveBox": point("Válvula con cajetín", "valveBox"),
            "regulator": point("Válvula reguladora", "regulator"),
            "checkValve": point("Válvula cheque", "checkValve"),
            "drainValve": point("Válvula de desagüe", "drainValve"),
            "airValve": point("Válvula de aire", "airValve"),
            "hydrant": point("Hidrante", "hydrant"),
            "macroMeter": point("Macromedidor", "macroMeter"),
            "pending": point("Elemento por confirmar", "pending"),
        },
    }


def generate(source_pdf: Path, output_path: Path):
    with pdfplumber.open(source_pdf) as document:
        if len(document.pages) != 1:
            raise ValueError("Se esperaba una sola página")
        page = document.pages[0]
        pipe_segments = []
        for color, color_name, diameter in ((BLUE, "blue", 110), (RED, "red", 90)):
            selected = [
                item for item in page.curves
                if color_tuple(item.get("stroking_color")) == color and item.get("fill")
            ]
            if len(selected) % 2:
                raise ValueError(f"Cantidad impar de semipolígonos {color_name}: {len(selected)}")
            for index in range(0, len(selected), 2):
                start, end = centerline(selected[index:index + 2])
                if np.linalg.norm(end - start) > 1:
                    pipe_segments.append({"color": color_name, "diameter": diameter, "start": start, "end": end})
        pond_curves = [
            item for item in page.curves
            if color_tuple(item.get("stroking_color")) == GREEN and not item.get("fill") and len(item.get("pts", [])) >= 4
        ]
        boundary_rects = [
            item for item in page.rects
            if color_tuple(item.get("stroking_color")) == GREEN and not item.get("fill")
        ]
        groups = yellow_groups(page)
        page_height = float(page.height)

    if Counter(segment["color"] for segment in pipe_segments) != Counter({"red": 67, "blue": 29}):
        raise ValueError("El inventario geométrico de tuberías cambió")
    tramos = extract_tramos(source_pdf, page_height)
    if Counter(tramo["diameter"] for tramo in tramos) != Counter({110: 19, 90: 17, 200: 1}):
        raise ValueError("El inventario de rótulos de tramos cambió")

    boundary_rects.sort(key=lambda item: (item["x1"] - item["x0"]) * (item["bottom"] - item["top"]), reverse=True)
    frame = boundary_rects[0]
    boundary = np.asarray([
        (frame["x0"], frame["top"]), (frame["x0"], frame["bottom"]),
        (frame["x1"], frame["bottom"]), (frame["x1"], frame["top"]),
    ], dtype=float)
    pond = np.asarray(pond_curves[0]["pts"][:-1], dtype=float)
    boundary_controls = {"P4": boundary[0], "P1": boundary[1], "P2": boundary[2], "P3": boundary[3]}
    outer_source = np.asarray([boundary_controls[f"P{i}"] for i in range(1, 5)])
    outer_target = np.asarray([CONTROLS[f"P{i}"] for i in range(1, 5)])
    initial, outer_rms = affine_fit(outer_source, outer_target)
    pond_controls = {}
    inverse = np.linalg.inv(np.vstack((initial.T, np.array((0.0, 0.0, 1.0)))))
    for number in range(5, 9):
        target = np.array((*CONTROLS[f"P{number}"], 1.0)) @ inverse.T
        pond_controls[f"P{number}"] = pond[int(np.argmin(np.linalg.norm(pond - target[:2], axis=1)))]
    all_controls = boundary_controls | pond_controls
    source_control = np.asarray([all_controls[f"P{i}"] for i in range(1, 9)])
    target_control = np.asarray([CONTROLS[f"P{i}"] for i in range(1, 9)])
    transform, rms = affine_fit(source_control, target_control)
    if rms > 0.05 or outer_rms > 0.05:
        raise ValueError(f"Control geográfico fuera de tolerancia: {rms:.4f} m")

    origin_east = min(value[0] for value in CONTROLS.values()) - DISPLAY_MARGIN_METERS
    origin_north = max(value[1] for value in CONTROLS.values()) + DISPLAY_MARGIN_METERS

    def to_display(point):
        east, north = apply_affine(point, transform)
        return np.array(((east - origin_east) * UNITS_PER_METER, (origin_north - north) * UNITS_PER_METER))

    # El rótulo ap-D identifica el único tramo rojo de 200 mm, junto al macromedidor.
    tramo_200 = next(tramo for tramo in tramos if tramo["diameter"] == 200)
    red_candidates = [(point_segment_distance(tramo_200["labelPoint"], segment["start"], segment["end"]), index) for index, segment in enumerate(pipe_segments) if segment["color"] == "red"]
    _, external_index = min(red_candidates)
    pipe_segments[external_index]["diameter"] = 200

    # Correspondencia global uno-a-uno entre los 37 rótulos y su geometría más próxima.
    assignments = {}
    assignment_distances = {}
    available = set(range(len(pipe_segments)))
    ordered_tramos = sorted(tramos, key=lambda item: 0 if item["diameter"] == 200 else -(item["meters"] or 0))
    for tramo in ordered_tramos:
        candidates = []
        for index in available:
            segment = pipe_segments[index]
            if segment["diameter"] != tramo["diameter"]:
                continue
            distance = point_segment_distance(tramo["labelPoint"], segment["start"], segment["end"])
            candidates.append((distance, index))
        if not candidates:
            raise ValueError(f"No existe geometría disponible para {tramo['identifier']}")
        distance, index = min(candidates)
        assignments[index] = tramo
        assignment_distances[tramo["identifier"]] = distance
        available.remove(index)
    if max(assignment_distances.values()) > 120:
        raise ValueError(
            f"Un rótulo quedó demasiado lejos de su tramo: {max(assignment_distances.values()):.2f} pt"
        )

    endpoints = []
    for segment_index, segment in enumerate(pipe_segments):
        endpoints.extend(((segment_index, 0, segment["start"]), (segment_index, 1, segment["end"])))
    # Los extremos que de verdad se tocan quedan a 0,3 pt; el siguiente salto
    # está en 28 pt. Un radio de 18 pt equivalía a 4 m: unía accesorios
    # separados por metros y, en los tramos cortos, los dos extremos de un mismo
    # tramo caían en el mismo nodo y lo convertían en un bucle.
    endpoint_dsu = DSU(len(endpoints))
    for left in range(len(endpoints)):
        for right in range(left + 1, len(endpoints)):
            if endpoints[left][0] == endpoints[right][0]:
                continue  # un tramo nunca puede empezar y terminar en el mismo accesorio
            if np.linalg.norm(endpoints[left][2] - endpoints[right][2]) <= ENDPOINT_SNAP_POINTS:
                endpoint_dsu.union(left, right)
    endpoint_groups = defaultdict(list)
    for index in range(len(endpoints)):
        endpoint_groups[endpoint_dsu.find(index)].append(index)
    ordered_endpoint_groups = sorted(endpoint_groups.values(), key=lambda values: tuple(np.mean([endpoints[i][2] for i in values], axis=0)[::-1]))

    nodes = []
    node_id_by_endpoint = {}
    node_pdf_by_id = {}
    for number, members in enumerate(ordered_endpoint_groups, 1):
        node_id = f"APN-{number:03d}"
        pdf_point = np.mean([endpoints[index][2] for index in members], axis=0)
        display = to_display(pdf_point)
        incident = [pipe_segments[endpoints[index][0]] for index in members]
        degree = len(members)
        element_type = "junction"
        display_name = "Unión geométrica"
        if degree == 1:
            element_type, display_name = "cap", f"Tapón {incident[0]['diameter']}"
        elif degree == 3:
            diameters = sorted({segment["diameter"] for segment in incident})
            if len(diameters) == 1:
                element_type, display_name = "teeEqual", f"Tee igual {diameters[0]}"
            else:
                element_type, display_name = "teeReducer", f"Tee{max(diameters)}/{min(diameters)}"
        elif degree >= 4:
            element_type, display_name = "cross", f"Cruz {max(segment['diameter'] for segment in incident)}"
        elif degree == 2:
            vectors = []
            for endpoint_index in members:
                segment_index, side, _point = endpoints[endpoint_index]
                segment = pipe_segments[segment_index]
                other = segment["end"] if side == 0 else segment["start"]
                vector = other - pdf_point
                vectors.append(vector / (np.linalg.norm(vector) or 1))
            angle = math.degrees(math.acos(float(np.clip(np.dot(vectors[0], vectors[1]), -1, 1))))
            if 72 <= angle <= 108:
                element_type, display_name = "elbow90", f"Codo 90° {incident[0]['diameter']}"
            elif 112 <= angle <= 155:
                element_type, display_name = "elbow135", f"Codo 135° {incident[0]['diameter']}"
            elif angle < 165:
                element_type, display_name = "semielbow", f"Semicodo {incident[0]['diameter']}"
        node = {
            "id": node_id, "x": round(float(display[0]), 2), "y": round(float(display[1]), 2),
            "radius": 4.2, "elementType": element_type, "displayName": display_name,
            "showLabel": False, "hidden": element_type == "junction", "networkType": "AAPP",
        }
        nodes.append(node)
        node_pdf_by_id[node_id] = pdf_point
        for endpoint_index in members:
            node_id_by_endpoint[endpoint_index] = node_id

    lines = []
    segment_endpoint_index = {(segment_index, side): segment_index * 2 + side for segment_index in range(len(pipe_segments)) for side in (0, 1)}
    for index, segment in enumerate(pipe_segments):
        start = to_display(segment["start"])
        end = to_display(segment["end"])
        tramo = assignments.get(index)
        diameter = int(segment["diameter"])
        kind = "collector" if diameter in (110, 200) else "branch"
        element_type = "externalPipe" if diameter == 200 else ("mainPipe" if diameter == 110 else "distributionPipe")
        identifier = tramo["identifier"] if tramo else f"AP-GEO-{index + 1:03d}"
        # La longitud sale de la geometría georreferenciada, de accesorio a
        # accesorio. El rótulo del plano se conserva solo como referencia: su
        # asignación es un heurístico de cercanía y no concuerda con el trazo.
        meters = float(np.linalg.norm(end - start)) / UNITS_PER_METER
        line = {
            "id": f"AAPP-{index + 1:03d}", "name": identifier,
            "from": node_id_by_endpoint[segment_endpoint_index[(index, 0)]],
            "to": node_id_by_endpoint[segment_endpoint_index[(index, 1)]],
            "meters": round(meters, 2),
            "kind": kind, "elementType": element_type, "networkType": "AAPP",
            "diameter": str(diameter), "identifier": identifier,
            "flow": tramo["flow"] if tramo else None,
            "d": f"M {start[0]:.2f} {start[1]:.2f} L {end[0]:.2f} {end[1]:.2f}",
            "planMeters": tramo["meters"] if tramo else None,
            "hasDistance": True,
            "showDistanceLabel": False,
            "excludeFromProgress": diameter == 200,
        }
        lines.append(line)

    runs = build_runs(lines)
    run_by_line = {line_id: run for run in runs for line_id in run["lines"]}
    for line in lines:
        run = run_by_line[line["id"]]
        line["runId"] = run["id"]
        # Un tramo, un rótulo: lo lleva la línea sobre la que cae el punto medio.
        line["showDistanceLabel"] = run["labelLineId"] == line["id"]

    # Ocho conjuntos grandes son los hidrantes confirmados (M + elemento perforado).
    hydrant_groups = sorted((group for group in groups if group["count"] >= 500), key=lambda group: (group["center"][1], group["center"][0]))
    if len(hydrant_groups) != 8:
        raise ValueError(f"Se esperaban 8 hidrantes y se encontraron {len(hydrant_groups)}")
    special_nodes = []
    for number, group in enumerate(hydrant_groups, 1):
        center = to_display(group["center"])
        special_nodes.append({
            "id": f"H-{number:02d}", "x": round(float(center[0]), 2), "y": round(float(center[1]), 2),
            "radius": 6.5, "elementType": "hydrant", "displayName": "Hidrante",
            # El círculo con la H ya lo identifica: el rótulo permanente sobra.
            # Sigue apareciendo con el filtro «ID Accesorios».
            "showLabel": False, "networkType": "AAPP",
        })
        # El conjunto del hidrante se representa con su círculo: la válvula queda
        # en el inventario pero no se dibuja, porque a 2,5 m se encimaba con él.
        special_nodes.append({
            "id": f"VH-{number:02d}", "x": round(float(center[0] - 8), 2), "y": round(float(center[1]), 2),
            "radius": 4.2, "elementType": "valve", "displayName": "Válvula de hidrante",
            "showLabel": False, "hidden": True, "networkType": "AAPP",
        })

    def add_groups_by_count(counts, element_type, prefix, display_name):
        selected = sorted((group for group in groups if group["count"] in counts), key=lambda group: (group["center"][1], group["center"][0]))
        for number, group in enumerate(selected, 1):
            center = to_display(group["center"])
            special_nodes.append({
                "id": f"{prefix}-{number:02d}", "x": round(float(center[0]), 2), "y": round(float(center[1]), 2),
                "radius": 4.5, "elementType": element_type, "displayName": display_name,
                "showLabel": False, "networkType": "AAPP",
            })

    add_groups_by_count({53}, "valve", "V", "Válvula")
    add_groups_by_count({67, 84}, "drainValve", "VD", "Válvula de desagüe")
    add_groups_by_count({112}, "airValve", "VA", "Válvula de aire")
    add_groups_by_count({320}, "macroMeter", "MD", "Macromedidor")
    add_groups_by_count({55}, "regulator", "VR", "Válvula reguladora")
    nodes.extend(special_nodes)

    # Un tubo que muere contra una válvula o un hidrante no lleva tapón: ese
    # extremo es la conexión al aparato, y el símbolo del aparato ya lo dice.
    # Dibujar ambos los encimaba.
    device_points = [
        (node["x"], node["y"]) for node in special_nodes
        if node["elementType"] in DEVICE_TYPES and not node.get("hidden")
    ]
    for node in nodes:
        if node["elementType"] != "cap":
            continue
        if any(math.dist((node["x"], node["y"]), point) <= CAP_TO_DEVICE_UNITS
               for point in device_points):
            node["elementType"] = "junction"
            node["displayName"] = "Conexión a aparato"
            node["hidden"] = True

    # C-02: configuración atípica junto a ap-110/ap-49; no se inventa accesorio.
    relevant = [line for line in lines if line["identifier"] in {"ap-110", "ap-49"}]
    comment_point = np.mean([
        np.mean((np.array((float(line["d"].split()[1]), float(line["d"].split()[2]))), np.array((float(line["d"].split()[4]), float(line["d"].split()[5])))), axis=0)
        for line in relevant
    ], axis=0)
    comments = [{
        "id": "C-02", "x": round(float(comment_point[0]), 2), "y": round(float(comment_point[1]), 2),
        "text": "C-02 · Confirmar en campo/diseño la configuración de la unión entre ap-110 y ap-49. No se contabiliza accesorio hasta recibir confirmación.",
    }]

    display_boundary = [to_display(point) for point in boundary]
    display_pond = [to_display(point) for point in pond]

    def as_points(values):
        return [{"x": round(float(p[0]), 2), "y": round(float(p[1]), 2)} for p in values]

    # Misma forma que el modelo AALL: el visor dibuja lindero, estanque y capa
    # satelital sin necesitar una rama propia para AAPP.
    georeference = build_georeference(origin_east, origin_north, rms, source_pdf.name)
    site_geometry = {
        "crs": "EPSG:32717",
        "source": "project-pdf-georeferenced",
        "sourceFile": source_pdf.name,
        "controlRmsMeters": round(rms, 6),
        "boundary": {"name": "Lindero Costanera Acacias", "points": as_points(display_boundary)},
        "areas": [{"name": "Estanque", "points": as_points(display_pond)}],
        "controlPoints": [
            {
                "id": label,
                "x": round((CONTROLS[label][0] - origin_east) * UNITS_PER_METER, 2),
                "y": round((origin_north - CONTROLS[label][1]) * UNITS_PER_METER, 2),
                "east": CONTROLS[label][0],
                "north": CONTROLS[label][1],
                "feature": "boundary" if label in ("P1", "P2", "P3", "P4") else "pond",
            }
            for label in sorted(CONTROLS)
        ],
        "satellite": build_satellite_layer(origin_east, origin_north),
    }
    extent_points = [*display_boundary, *display_pond, *[np.array((node["x"], node["y"])) for node in nodes]]
    minimum = np.min(extent_points, axis=0)
    maximum = np.max(extent_points, axis=0)
    view_padding = 55.0
    view_box = {
        "x": round(float(minimum[0] - view_padding), 2),
        "y": round(float(minimum[1] - view_padding), 2),
        "width": round(float(maximum[0] - minimum[0] + view_padding * 2), 2),
        "height": round(float(maximum[1] - minimum[1] + view_padding * 2), 2),
    }
    definition = build_definition()
    line_settings = {
        line["id"]: {"meters": line["meters"], "identifier": line["identifier"], "diameter": line["diameter"]}
        for line in lines
    }
    project = {
        "id": "builtin-costanera-acacias-aapp",
        "key": "costanera-acacias-aapp",
        "revision": 7,
        "name": "Costanera_Acacias_AAPP",
        "networkType": "AAPP",
        "source": "builtin",
        "note": "Modelo AAPP extraído del plano COSTANERA AAPP githubprogram (3)-Model.pdf",
        "metadata": {
            "sourceFile": source_pdf.name,
            "pipeGeometryCount": len(lines),
            "runCount": len(runs),
            "labeledTramoCount": len(tramos),
            "accessoryNodeCount": len([node for node in nodes if not node.get("hidden")]),
            "hydrantCount": 8,
            "controlPointCount": 8,
            "controlRmsMeters": round(rms, 6),
            "endpointSnapPoints": ENDPOINT_SNAP_POINTS,
            "shortRunMeters": SHORT_RUN_METERS,
            # Longitudes tomadas de la geometría georreferenciada, no del rótulo.
            "metersByDiameter": {
                diameter: round(sum(run["meters"] for run in runs if run["diameter"] == diameter), 2)
                for diameter in sorted({run["diameter"] for run in runs}, key=int, reverse=True)
            },
            "contractMeters": round(sum(run["meters"] for run in runs if not run["excludeFromProgress"]), 2),
            "maximumLabelOffsetPdfPoints": round(max(assignment_distances.values()), 3),
        },
        "model": {"name": "Costanera_Acacias_AAPP", "networkType": "AAPP"},
        "snapshot": {
            "schemaVersion": 2,
            "network": {
                "type": "AAPP", "definition": definition, "nodes": nodes, "lines": lines,
                "runs": runs,
                "viewBox": view_box, "georeference": georeference,
                "routeEdges": [], "siteGeometry": site_geometry,
            },
            "state": {
                "activities": [], "comments": comments, "lineSettings": line_settings,
                "nodeSettings": {}, "filters": {"collector": True, "branch": True, "points": True, "completed": True},
            },
            "metadata": {
                "projectName": "Costanera_Acacias_AAPP", "sourceFile": source_pdf.name,
                "tramoCounts": {"110": 19, "90": 17, "200": 1},
                "tramoTotalsMeters": {"110": 1295.34, "90": 1626.26},
                "hydrantCount": 8, "excludedHydraulicNodes": ["J-64", "J-65", "J-67"],
                "maximumLabelOffsetPdfPoints": round(max(assignment_distances.values()), 3),
                "notes": [
                    "La tubería de 200 mm comienza en el macromedidor y se mantiene fuera del avance de la red.",
                    "M y el elemento circular perforado se modelan como un solo hidrante; su válvula se conserva separada.",
                    "La desviación azul sutil posterior a ap-113 se acepta sin accesorio.",
                    "La unión ap-110/ap-49 permanece como comentario C-02 pendiente de confirmación.",
                ],
            },
        },
    }
    payload = json.dumps(project, ensure_ascii=False, separators=(",", ":"))
    javascript = f"""(() => {{
  const project = {payload};
  const existing = Array.isArray(window.RED_NETWORK_BUILTIN_PROJECTS)
    ? window.RED_NETWORK_BUILTIN_PROJECTS.slice()
    : [];
  const withoutCurrent = existing.filter(item => item?.id !== project.id);
  window.RED_NETWORK_BUILTIN_PROJECTS = Object.freeze([...withoutCurrent, project]);
}})();
"""
    output_path.write_text(javascript, encoding="utf-8")
    return project


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(r"C:\Users\Mario\Desktop\COSTANERA AAPP githubprogram (3)-Model.pdf"))
    parser.add_argument("--output", type=Path, default=Path("costanera-acacias-aapp.js"))
    args = parser.parse_args()
    project = generate(args.source, args.output)
    print(json.dumps({
        "output": str(args.output),
        "nodes": len(project["snapshot"]["network"]["nodes"]),
        "lines": len(project["snapshot"]["network"]["lines"]),
        "comments": len(project["snapshot"]["state"]["comments"]),
        "controlRmsMeters": project["snapshot"]["network"]["siteGeometry"]["controlRmsMeters"],
        "maximumLabelOffsetPdfPoints": project["metadata"]["maximumLabelOffsetPdfPoints"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
