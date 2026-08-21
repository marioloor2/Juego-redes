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
# Los tramos menores a este umbral se conservan completos, pero el visor solo
# muestra su cota cuando el zoom le da un tamaño de lectura suficiente.
SHORT_LABEL_METERS = 15.0
# Solo se usa para asociar el rótulo histórico con su geometría original. No
# participa en la formación ni en la longitud de los nuevos tramos.
PLAN_REFERENCE_LENGTH_WEIGHT = 3.0
# Aparatos que representan por sí solos el final de la tubería.
DEVICE_TYPES = {"valve", "valveBox", "checkValve", "hydrant", "macroMeter", "regulator", "airValve", "drainValve"}
# Se conservan en el inventario para afinarlos después, pero no se dibujan en
# esta etapa base del modelo.
DEFERRED_VALVE_TYPES = {"valve", "valveBox", "regulator", "checkValve", "drainValve", "airValve"}
# Un extremo a menos de esta distancia de un aparato es su conexión, no un tapón.
CAP_TO_DEVICE_UNITS = 16.0
# Los trazos cortos del montaje detallado se omiten al mostrar un hidrante como
# marcador. No se sustituyen por ramales nuevos ni se altera la red restante.
HYDRANT_ASSEMBLY_MAX_METERS = 8.0
HYDRANT_ASSEMBLY_SEARCH_UNITS = 20.0


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


def minimum_cost_assignment(costs):
    """Asigna cada fila a una columna distinta con costo total mínimo."""
    row_count = len(costs)
    column_count = len(costs[0]) if row_count else 0
    if row_count > column_count:
        raise ValueError("No hay suficientes tramos para asignar las referencias")
    row_potential = [0.0] * (row_count + 1)
    column_potential = [0.0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    path = [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        matched_row[0] = row
        minimum = [float("inf")] * (column_count + 1)
        used = [False] * (column_count + 1)
        column = 0
        while True:
            used[column] = True
            active_row = matched_row[column]
            delta = float("inf")
            next_column = 0
            for candidate in range(1, column_count + 1):
                if used[candidate]:
                    continue
                reduced = (costs[active_row - 1][candidate - 1]
                           - row_potential[active_row]
                           - column_potential[candidate])
                if reduced < minimum[candidate]:
                    minimum[candidate] = reduced
                    path[candidate] = column
                if minimum[candidate] < delta:
                    delta = minimum[candidate]
                    next_column = candidate
            for candidate in range(column_count + 1):
                if used[candidate]:
                    row_potential[matched_row[candidate]] += delta
                    column_potential[candidate] -= delta
                else:
                    minimum[candidate] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            previous = path[column]
            matched_row[column] = matched_row[previous]
            column = previous
            if column == 0:
                break
    assignment = [-1] * row_count
    for column in range(1, column_count + 1):
        if matched_row[column]:
            assignment[matched_row[column] - 1] = column - 1
    return assignment


def assign_plan_references(runs, tramos, lines):
    """Conserva cada código del plano como referencia localizable.

    Primero se asocia cada rótulo a una geometría original distinta y luego se
    traslada esa referencia al tramo por accesorios que contiene la geometría.
    Varias referencias históricas pueden terminar en un mismo tramo nuevo.
    """
    by_id = {line["id"]: line for line in lines}
    run_by_line = {line_id: run for run in runs for line_id in run["lines"]}
    segments = {line["id"]: line_endpoints(line) for line in lines}

    report = []
    for diameter in sorted({str(tramo["diameter"]) for tramo in tramos}, key=int):
        diameter_tramos = [tramo for tramo in tramos if str(tramo["diameter"]) == diameter]
        diameter_lines = [line for line in lines if line["diameter"] == diameter]
        distances = [[
            point_segment_distance(tramo["labelDisplayPoint"], *segments[line["id"]])
            / UNITS_PER_METER
            for line in diameter_lines
        ] for tramo in diameter_tramos]
        costs = [[
            distance + (
                abs(tramo["meters"] - diameter_lines[line_index]["meters"])
                * PLAN_REFERENCE_LENGTH_WEIGHT
                if tramo["meters"] is not None else 0.0
            )
            for line_index, distance in enumerate(distances[tramo_index])
        ] for tramo_index, tramo in enumerate(diameter_tramos)]
        for tramo_index, (tramo, line_index) in enumerate(
                zip(diameter_tramos, minimum_cost_assignment(costs))):
            source_line = diameter_lines[line_index]
            run = run_by_line[source_line["id"]]
            distance = distances[tramo_index][line_index]
            gap = None if tramo["meters"] is None else abs(tramo["meters"] - run["meters"])
            reference = {
                "identifier": tramo["identifier"],
                "sourceLineId": source_line["id"],
                "planMeters": tramo["meters"],
                "flow": tramo.get("flow"),
                "labelDistanceMeters": round(distance, 2),
            }
            run["planReferences"].append(reference)
            report.append({
                "code": tramo["identifier"], "sourceLine": source_line["id"], "run": run["id"],
                "planMeters": tramo["meters"], "meters": run["meters"],
                "gap": None if gap is None else round(gap, 2),
                "labelDistanceMeters": round(distance, 2),
            })
    if len(report) != len(tramos):
        raise ValueError(f"Quedaron referencias sin tramo: {len(tramos) - len(report)}")
    return report


def build_runs(lines, nodes):
    """Forma tramos estrictamente de accesorio a accesorio.

    Solo se atraviesa una unión geométrica oculta de grado dos y del mismo
    diámetro. Cualquier accesorio real, aunque esté en un tramo muy corto,
    separa dos tramos distintos.
    """
    by_id = {line["id"]: line for line in lines}
    ends = {line["id"]: line_endpoints(line) for line in lines}
    incident = defaultdict(list)
    for line in lines:
        incident[line["from"]].append(line["id"])
        incident[line["to"]].append(line["id"])

    parent = {line["id"]: line["id"] for line in lines}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    nodes_by_id = {node["id"]: node for node in nodes}
    for node_id, connected in incident.items():
        node = nodes_by_id.get(node_id)
        if not node or node.get("elementType") != "junction" or not node.get("hidden"):
            continue
        if len(connected) != 2:
            continue
        first, second = connected
        if by_id[first]["diameter"] != by_id[second]["diameter"]:
            continue
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    members = defaultdict(list)
    for line_id in parent:
        members[find(line_id)].append(line_id)

    runs = []
    for number, (root, group) in enumerate(
            sorted(members.items(), key=lambda item: min(item[1])), 1):
        ordered = order_run(group, by_id)
        total = sum(by_id[m]["meters"] for m in ordered)
        label_line, label_point = run_midpoint(ordered, by_id, ends, total)
        # Dirección de la tubería en el punto de la cota: el rótulo se dibuja con
        # esa misma inclinación, para que se lea junto al tramo que mide.
        start, end = ends[label_line]
        angle = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
        runs.append({
            "id": f"TR-{number:03d}",
            "diameter": by_id[root]["diameter"],
            "meters": round(total, 2),
            "lines": ordered,
            "labelLineId": label_line,
            "labelPoint": {"x": round(float(label_point[0]), 2),
                           "y": round(float(label_point[1]), 2)},
            "labelAngle": round(float(angle), 2),
            "identifier": f"AAPP-TR-{number:03d}",
            "planReferences": [],
            "planMeters": None,
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

    # Los rótulos del plano se emparejan más adelante, ya formados los tramos.
    # Son referencias de búsqueda y nunca gobiernan la segmentación.
    for tramo in tramos:
        tramo["labelDisplayPoint"] = to_display(tramo["labelPoint"])

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
        diameter = int(segment["diameter"])
        kind = "collector" if diameter in (110, 200) else "branch"
        element_type = "externalPipe" if diameter == 200 else ("mainPipe" if diameter == 110 else "distributionPipe")
        # La longitud sale de la geometría georreferenciada. Las líneas que solo
        # se separan por una unión técnica se agrupan después; los accesorios
        # reales siempre cortan el tramo.
        meters = float(np.linalg.norm(end - start)) / UNITS_PER_METER
        identifier = f"AP-GEO-{index + 1:03d}"
        line = {
            "id": f"AAPP-{index + 1:03d}", "name": identifier,
            "from": node_id_by_endpoint[segment_endpoint_index[(index, 0)]],
            "to": node_id_by_endpoint[segment_endpoint_index[(index, 1)]],
            "meters": round(meters, 2),
            "kind": kind, "elementType": element_type, "networkType": "AAPP",
            "diameter": str(diameter), "identifier": identifier,
            "flow": None,
            "d": f"M {start[0]:.2f} {start[1]:.2f} L {end[0]:.2f} {end[1]:.2f}",
            "planMeters": None,
            "hasDistance": True,
            "showDistanceLabel": False,
            "excludeFromProgress": diameter == 200,
        }
        lines.append(line)

    runs = build_runs(lines, nodes)
    plan_assignment = assign_plan_references(runs, tramos, lines)
    run_by_line = {line_id: run for run in runs for line_id in run["lines"]}
    for line in lines:
        run = run_by_line[line["id"]]
        line["runId"] = run["id"]
        # El ID del modelo nace de la segmentación por accesorios. Los códigos
        # originales permanecen aparte como referencias de búsqueda.
        line["identifier"] = run["identifier"]
        line["name"] = run["identifier"]
        line["planReferences"] = run["planReferences"]
        line["flow"] = None
        line["planMeters"] = None
        # Un tramo, un rótulo: lo lleva la línea sobre la que cae el punto medio.
        line["showDistanceLabel"] = run["labelLineId"] == line["id"]

    # Ocho conjuntos grandes son los hidrantes confirmados (M + elemento perforado).
    # Se representan exclusivamente como marcadores H en su posición original.
    # Se omite solo el montaje corto propio del hidrante; la geometría de todas
    # las tuberías se conserva y no se crea ningún ramal de sustitución.
    hydrant_groups = sorted((group for group in groups if group["count"] >= 500), key=lambda group: (group["center"][1], group["center"][0]))
    if len(hydrant_groups) != 8:
        raise ValueError(f"Se esperaban 8 hidrantes y se encontraron {len(hydrant_groups)}")

    hydrant_centers = [to_display(group["center"]) for group in hydrant_groups]
    line_geometry = {line["id"]: line_endpoints(line) for line in lines}
    node_by_id = {node["id"]: node for node in nodes}
    endpoint_pair_counts = Counter(
        frozenset((line["from"], line["to"])) for line in lines
    )
    primary_assembly_by_hydrant = {}
    assigned_primary_lines = set()
    for number, center in enumerate(hydrant_centers, 1):
        candidates = [
            line for line in lines
            if line["id"] not in assigned_primary_lines
            and line["meters"] <= HYDRANT_ASSEMBLY_MAX_METERS
            and point_segment_distance(center, *line_geometry[line["id"]]) <= HYDRANT_ASSEMBLY_SEARCH_UNITS
            and (
                endpoint_pair_counts[frozenset((line["from"], line["to"]))] > 1
                or any(
                    node_by_id[node_id].get("hidden")
                    or node_by_id[node_id]["elementType"] in {"cap", "junction"}
                    for node_id in (line["from"], line["to"])
                )
            )
        ]
        if not candidates:
            raise ValueError(f"No se encontró el montaje original del hidrante H-{number:02d}")
        primary = min(candidates, key=lambda line: (
            point_segment_distance(center, *line_geometry[line["id"]]),
            line["meters"],
            line["id"],
        ))
        primary_assembly_by_hydrant[number] = primary
        assigned_primary_lines.add(primary["id"])

    assembly_owner = {}
    for number, primary in primary_assembly_by_hydrant.items():
        primary_nodes = frozenset((primary["from"], primary["to"]))
        for line in lines:
            if frozenset((line["from"], line["to"])) == primary_nodes:
                assembly_owner[line["id"]] = number

    hydrant_assembly_line_ids = set(assembly_owner)
    for line in lines:
        owner = assembly_owner.get(line["id"])
        if owner is None:
            continue
        line["visualHidden"] = True
        line["showDistanceLabel"] = False
        line["excludeFromProgress"] = True
        line["hydrantAssemblyId"] = f"H-{owner:02d}"

    incident_lines = defaultdict(list)
    for line in lines:
        incident_lines[line["from"]].append(line)
        incident_lines[line["to"]].append(line)
    for line_id in hydrant_assembly_line_ids:
        line = next(item for item in lines if item["id"] == line_id)
        for node_id in (line["from"], line["to"]):
            # Solo desaparecen las piezas exclusivas del montaje. Un accesorio
            # compartido con la red permanece exactamente donde estaba.
            if any(item["id"] not in hydrant_assembly_line_ids for item in incident_lines[node_id]):
                continue
            node = node_by_id[node_id]
            node["elementType"] = "junction"
            node["displayName"] = "Montaje de hidrante omitido"
            node["hidden"] = True

    hydrant_outlets = []
    special_nodes = []
    for number, center in enumerate(hydrant_centers, 1):
        hydrant_id = f"H-{number:02d}"
        special_nodes.append({
            "id": hydrant_id, "x": round(float(center[0]), 2), "y": round(float(center[1]), 2),
            "radius": 8.5, "elementType": "hydrant", "displayName": "Hidrante",
            # El círculo con la H ya lo identifica: el rótulo permanente sobra.
            # Sigue apareciendo con el filtro «ID Accesorios».
            "showLabel": False, "networkType": "AAPP",
        })
        # La válvula se conserva solo en inventario para una etapa posterior.
        special_nodes.append({
            "id": f"VH-{number:02d}", "x": round(float(center[0]), 2), "y": round(float(center[1]), 2),
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
                "showLabel": False, "hidden": element_type in DEFERRED_VALVE_TYPES,
                "networkType": "AAPP",
            })

    add_groups_by_count({53}, "valve", "V", "Válvula")
    add_groups_by_count({67, 84}, "drainValve", "VD", "Válvula de desagüe")
    add_groups_by_count({112}, "airValve", "VA", "Válvula de aire")
    add_groups_by_count({320}, "macroMeter", "MD", "Macromedidor")
    add_groups_by_count({55}, "regulator", "VR", "Válvula reguladora")
    nodes.extend(special_nodes)

    # Los hidrantes son marcadores independientes y no pueden reclasificar los
    # extremos de la red original. Otros aparatos confirmados sí conservan su
    # tratamiento de conexión.
    device_points = [
        (node["x"], node["y"]) for node in special_nodes
        if node["elementType"] in DEVICE_TYPES
        and node["elementType"] != "hydrant"
        and (not node.get("hidden") or not node["id"].startswith("VH-"))
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
    relevant = [
        line for line in lines
        if {reference["identifier"] for reference in line.get("planReferences", [])}
        & {"ap-110", "ap-49"}
    ]
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
        "revision": 16,
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
            "simplifiedHydrantOutletCount": 0,
            "hydrantRepresentation": "marker-only",
            "omittedHydrantAssemblyLineCount": len(hydrant_assembly_line_ids),
            "controlPointCount": 8,
            "controlRmsMeters": round(rms, 6),
            "endpointSnapPoints": ENDPOINT_SNAP_POINTS,
            "shortLabelMeters": SHORT_LABEL_METERS,
            "segmentationRule": "accessory-to-accessory",
            "planReferenceCount": len(plan_assignment),
            "deferredValveCount": len([
                node for node in nodes if node["elementType"] in DEFERRED_VALVE_TYPES
            ]),
            # Longitudes tomadas de la geometría georreferenciada, no del rótulo.
            "metersByDiameter": {
                diameter: round(sum(run["meters"] for run in runs if run["diameter"] == diameter), 2)
                for diameter in sorted({run["diameter"] for run in runs}, key=int, reverse=True)
            },
            "contractMeters": round(sum(run["meters"] for run in runs if not run["excludeFromProgress"]), 2),
            "maximumPlanLabelDistanceMeters": round(max(item["labelDistanceMeters"] for item in plan_assignment), 2),
        },
        "model": {"name": "Costanera_Acacias_AAPP", "networkType": "AAPP"},
        "snapshot": {
            "schemaVersion": 2,
            "network": {
                "type": "AAPP", "definition": definition, "nodes": nodes, "lines": lines,
                "runs": runs, "hydrantOutlets": hydrant_outlets,
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
                "maximumPlanLabelDistanceMeters": round(max(item["labelDistanceMeters"] for item in plan_assignment), 2),
                "notes": [
                    "La tubería de 200 mm comienza en el macromedidor y se mantiene fuera del avance de la red.",
                    "Los tramos se separan de accesorio a accesorio; los códigos AP del plano son solo referencias de búsqueda.",
                    "Cada hidrante se representa únicamente con un círculo H en su posición original; su montaje corto se omite sin añadir ramales ni alterar la geometría de la red restante.",
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
        "runs": project["metadata"]["runCount"],
        "contractMeters": project["metadata"]["contractMeters"],
        "maximumPlanLabelDistanceMeters": project["metadata"]["maximumPlanLabelDistanceMeters"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
