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
            "tubeModuleMeters": 6,
            "oneTotalLengthLabelPerLine": True,
            "labelEveryTubeModule": False,
            "tubeDivisionGuidesVisibleOnZoom": True,
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
    endpoint_dsu = DSU(len(endpoints))
    for left in range(len(endpoints)):
        for right in range(left + 1, len(endpoints)):
            if np.linalg.norm(endpoints[left][2] - endpoints[right][2]) <= 18:
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
        line = {
            "id": f"AAPP-{index + 1:03d}", "name": identifier,
            "from": node_id_by_endpoint[segment_endpoint_index[(index, 0)]],
            "to": node_id_by_endpoint[segment_endpoint_index[(index, 1)]],
            "meters": tramo["meters"] if tramo else None,
            "kind": kind, "elementType": element_type, "networkType": "AAPP",
            "diameter": str(diameter), "identifier": identifier,
            "flow": tramo["flow"] if tramo else None,
            "d": f"M {start[0]:.2f} {start[1]:.2f} L {end[0]:.2f} {end[1]:.2f}",
            "hasDistance": tramo is not None and tramo["meters"] is not None,
            "showDistanceLabel": tramo is not None and tramo["meters"] is not None,
            "excludeFromProgress": tramo is None or tramo["meters"] is None or diameter == 200,
        }
        lines.append(line)

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
            "showLabel": True, "networkType": "AAPP",
        })
        special_nodes.append({
            "id": f"VH-{number:02d}", "x": round(float(center[0] - 8), 2), "y": round(float(center[1]), 2),
            "radius": 4.2, "elementType": "valve", "displayName": "Válvula de hidrante",
            "showLabel": False, "networkType": "AAPP",
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
    site_geometry = {
        "source": "project-pdf-georeferenced",
        "controlRmsMeters": round(rms, 6),
        "boundary": [{"x": round(float(p[0]), 2), "y": round(float(p[1]), 2)} for p in display_boundary],
        "pond": [{"x": round(float(p[0]), 2), "y": round(float(p[1]), 2)} for p in display_pond],
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
        "revision": 2,
        "name": "Costanera_Acacias_AAPP",
        "networkType": "AAPP",
        "source": "builtin",
        "note": "Modelo AAPP extraído del plano COSTANERA AAPP githubprogram (3)-Model.pdf",
        "metadata": {
            "sourceFile": source_pdf.name,
            "pipeGeometryCount": len(lines),
            "labeledTramoCount": len(tramos),
            "accessoryNodeCount": len([node for node in nodes if not node.get("hidden")]),
            "hydrantCount": 8,
            "controlPointCount": 8,
            "controlRmsMeters": round(rms, 6),
            "maximumLabelOffsetPdfPoints": round(max(assignment_distances.values()), 3),
        },
        "model": {"name": "Costanera_Acacias_AAPP", "networkType": "AAPP"},
        "snapshot": {
            "schemaVersion": 2,
            "network": {
                "type": "AAPP", "definition": definition, "nodes": nodes, "lines": lines,
                "viewBox": view_box,
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
