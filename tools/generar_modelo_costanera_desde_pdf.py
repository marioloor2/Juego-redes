from __future__ import annotations

import argparse
import itertools
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pdfplumber


BLUE = (0.0, 0.0, 1.0)
RED = (1.0, 0.0, 0.0)
SITE_GREEN = (0.24706, 1.0, 0.0)
DISPLAY_UNITS_PER_METER = 5.7530408
DISPLAY_MARGIN_METERS = 50.0

CONTROL_COORDINATES = {
    "P1": (603434.2640, 9754607.7158),
    "P2": (603748.4870, 9754831.8839),
    "P3": (603434.3761, 9755254.6087),
    "P4": (603114.8758, 9755032.8162),
    "P5": (603524.4357, 9754815.0770),
    "P6": (603603.1132, 9754832.1520),
    "P7": (603561.1244, 9754887.7180),
    "P8": (603498.6957, 9754854.3130),
}


def color_tuple(value) -> tuple[float, ...]:
    return tuple(round(float(component), 5) for component in (value or ()))


def load_project(path: Path) -> dict:
    source = path.read_text(encoding="utf-8")
    match = re.search(r"const project = (\{.*?\n\});\n  if", source, re.S)
    if not match:
        raise ValueError(f"No se encontró el proyecto JSON en {path}")
    return json.loads(match.group(1))


def affine_fit(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, float]:
    matrix = np.column_stack((source, np.ones(len(source))))
    transform = np.linalg.lstsq(matrix, target, rcond=None)[0]
    residuals = matrix @ transform - target
    rms = float(np.sqrt(np.mean(np.sum(residuals**2, axis=1))))
    return transform, rms


def apply_affine(point, transform: np.ndarray) -> np.ndarray:
    return np.array([float(point[0]), float(point[1]), 1.0]) @ transform


def segment_cost(a, b, q, r) -> float:
    direct = float(np.linalg.norm(a - q) + np.linalg.norm(b - r))
    reverse = float(np.linalg.norm(a - r) + np.linalg.norm(b - q))
    return min(direct, reverse)


def orient_segment(a, b, q, r):
    direct = float(np.linalg.norm(a - q) + np.linalg.norm(b - r))
    reverse = float(np.linalg.norm(a - r) + np.linalg.norm(b - q))
    return (q, r) if direct <= reverse else (r, q)


def farthest_pair(points) -> tuple[np.ndarray, np.ndarray]:
    unique = np.unique(np.asarray(points, dtype=float), axis=0)
    distances = np.sum((unique[:, None, :] - unique[None, :, :]) ** 2, axis=2)
    first, second = np.unravel_index(np.argmax(distances), distances.shape)
    return unique[first], unique[second]


def inverse_utm_17s(easting: float, northing: float) -> tuple[float, float]:
    # WGS84 inverse UTM, specialized to zone 17 south.
    semi_major = 6378137.0
    eccentricity_squared = 0.00669437999014
    scale = 0.9996
    x = easting - 500000.0
    y = northing - 10000000.0
    longitude_origin = math.radians(-81.0)
    eccentricity_prime_squared = eccentricity_squared / (1 - eccentricity_squared)
    meridional_arc = y / scale
    mu = meridional_arc / (
        semi_major
        * (
            1
            - eccentricity_squared / 4
            - 3 * eccentricity_squared**2 / 64
            - 5 * eccentricity_squared**3 / 256
        )
    )
    e1 = (1 - math.sqrt(1 - eccentricity_squared)) / (
        1 + math.sqrt(1 - eccentricity_squared)
    )
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    sin_phi = math.sin(phi1)
    cos_phi = math.cos(phi1)
    tan_phi = math.tan(phi1)
    n1 = semi_major / math.sqrt(1 - eccentricity_squared * sin_phi**2)
    t1 = tan_phi**2
    c1 = eccentricity_prime_squared * cos_phi**2
    r1 = semi_major * (1 - eccentricity_squared) / (
        1 - eccentricity_squared * sin_phi**2
    ) ** 1.5
    d = x / (n1 * scale)
    latitude = phi1 - (n1 * tan_phi / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * eccentricity_prime_squared)
        * d**4
        / 24
        + (
            61
            + 90 * t1
            + 298 * c1
            + 45 * t1**2
            - 252 * eccentricity_prime_squared
            - 3 * c1**2
        )
        * d**6
        / 720
    )
    longitude = longitude_origin + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (
            5
            - 2 * c1
            + 28 * t1
            - 3 * c1**2
            + 8 * eccentricity_prime_squared
            + 24 * t1**2
        )
        * d**5
        / 120
    ) / cos_phi
    return math.degrees(longitude), math.degrees(latitude)


def web_mercator_pixel(easting: float, northing: float, zoom: int) -> np.ndarray:
    longitude, latitude = inverse_utm_17s(easting, northing)
    world = 256.0 * (2**zoom)
    x = (longitude + 180.0) / 360.0 * world
    latitude_radians = math.radians(latitude)
    y = (
        1.0
        - math.asinh(math.tan(latitude_radians)) / math.pi
    ) / 2.0 * world
    return np.array([x, y])


def rounded_point(point) -> dict[str, float]:
    return {"x": round(float(point[0]), 2), "y": round(float(point[1]), 2)}


def generate(source_pdf: Path, project_path: Path, output_path: Path) -> dict:
    project = load_project(project_path)
    network = project["snapshot"]["network"]
    old_nodes = {node["id"]: node for node in network["nodes"]}
    collectors = [
        line for line in network["lines"] if line.get("elementType") == "collector"
    ]
    ties = [line for line in network["lines"] if line.get("elementType") == "tie"]

    with pdfplumber.open(source_pdf) as document:
        if len(document.pages) != 1:
            raise ValueError("Se esperaba una sola página en el PDF de Costanera")
        page = document.pages[0]
        red_segments = [
            tuple(np.asarray(point, dtype=float) for point in item["pts"])
            for item in page.lines
            if color_tuple(item.get("stroking_color")) == RED
        ]
        blue_polygons = []
        for item in page.curves:
            if color_tuple(item.get("stroking_color")) != BLUE or not item.get("fill"):
                continue
            if math.hypot(item["x1"] - item["x0"], item["y1"] - item["y0"]) <= 20:
                continue
            blue_polygons.append(farthest_pair(item["pts"]))
        magenta_fills = [
            item
            for item in page.curves
            if color_tuple(item.get("stroking_color")) == (1.0, 0.0, 1.0)
            and item.get("fill")
        ]
        site_curves = [
            item
            for item in page.curves
            if color_tuple(item.get("stroking_color")) == SITE_GREEN
            and not item.get("fill")
            and len(item.get("pts", [])) >= 4
        ]

    if len(red_segments) != 67 or len(blue_polygons) != 62:
        raise ValueError(
            f"Inventario vectorial inesperado: {len(red_segments)} tirantes y "
            f"{len(blue_polygons)} semipolígonos de colector"
        )

    site_curves.sort(
        key=lambda item: (item["x1"] - item["x0"]) * (item["y1"] - item["y0"]),
        reverse=True,
    )
    boundary_pdf = np.asarray(site_curves[0]["pts"][:-1], dtype=float)
    pond_pdf = np.asarray(site_curves[1]["pts"][:-1], dtype=float)
    if len(boundary_pdf) != 4:
        raise ValueError("El lindero exterior no tiene cuatro vértices")

    boundary_controls = {
        "P4": boundary_pdf[0],
        "P1": boundary_pdf[1],
        "P2": boundary_pdf[2],
        "P3": boundary_pdf[3],
    }
    outer_pdf = np.asarray([boundary_controls[f"P{number}"] for number in range(1, 5)])
    outer_utm = np.asarray([CONTROL_COORDINATES[f"P{number}"] for number in range(1, 5)])
    initial_pdf_to_utm, outer_rms = affine_fit(outer_pdf, outer_utm)

    pond_controls = {}
    inverse_initial = np.linalg.inv(
        np.vstack((initial_pdf_to_utm.T, np.array([0.0, 0.0, 1.0])))
    )
    for number in range(5, 9):
        east, north = CONTROL_COORDINATES[f"P{number}"]
        predicted = np.array([east, north, 1.0]) @ inverse_initial.T
        distances = np.linalg.norm(pond_pdf - predicted[:2], axis=1)
        index = int(np.argmin(distances))
        if distances[index] > 0.2:
            raise ValueError(
                f"El punto P{number} no coincide con un vértice del estanque: "
                f"desfase {distances[index]:.3f} pt"
            )
        pond_controls[f"P{number}"] = pond_pdf[index]

    all_pdf_controls = np.asarray(
        [
            (boundary_controls | pond_controls)[f"P{number}"]
            for number in range(1, 9)
        ]
    )
    all_utm_controls = np.asarray(
        [CONTROL_COORDINATES[f"P{number}"] for number in range(1, 9)]
    )
    pdf_to_utm, control_rms = affine_fit(all_pdf_controls, all_utm_controls)
    determinant = float(np.linalg.det(pdf_to_utm[:2, :].T))
    if determinant >= 0:
        raise ValueError(
            "La orientación PDF a UTM no contiene la inversión vertical esperada del papel"
        )
    if control_rms > 0.02 or outer_rms > 0.02:
        raise ValueError(
            f"Residuos de control excesivos: exterior {outer_rms:.4f} m, "
            f"total {control_rms:.4f} m"
        )

    min_easting = min(value[0] for value in CONTROL_COORDINATES.values())
    max_northing = max(value[1] for value in CONTROL_COORDINATES.values())
    display_origin_east = min_easting - DISPLAY_MARGIN_METERS
    display_origin_north = max_northing + DISPLAY_MARGIN_METERS

    def pdf_to_display(point) -> np.ndarray:
        east, north = apply_affine(point, pdf_to_utm)
        return np.array(
            [
                (east - display_origin_east) * DISPLAY_UNITS_PER_METER,
                (display_origin_north - north) * DISPLAY_UNITS_PER_METER,
            ]
        )

    # Three unambiguous chamber centers establish correspondence with the draft.
    old_anchor_ids = ("CAM_AL_24", "CAM_AL_18", "CAM_AL_15")
    pdf_anchor_points = np.asarray(
        ((570.28, 351.56), (992.56, 593.48), (1145.08, 3007.88)),
        dtype=float,
    )
    old_anchor_points = np.asarray(
        [[old_nodes[node_id]["x"], old_nodes[node_id]["y"]] for node_id in old_anchor_ids]
    )
    old_to_pdf = np.linalg.solve(
        np.column_stack((old_anchor_points, np.ones(3))), pdf_anchor_points
    )

    assigned_blue = set()
    collector_page_segments = {}
    collector_node_observations = defaultdict(list)
    for line in collectors:
        predicted_from = apply_affine(
            (old_nodes[line["from"]]["x"], old_nodes[line["from"]]["y"]), old_to_pdf
        )
        predicted_to = apply_affine(
            (old_nodes[line["to"]]["x"], old_nodes[line["to"]]["y"]), old_to_pdf
        )
        candidates = sorted(
            (
                segment_cost(predicted_from, predicted_to, first, second),
                index,
                first,
                second,
            )
            for index, (first, second) in enumerate(blue_polygons)
            if index not in assigned_blue
        )[:2]
        if len(candidates) != 2 or candidates[-1][0] > 5:
            raise ValueError(f"No se pudo reconstruir el colector {line['id']}")
        oriented = []
        for _, index, first, second in candidates:
            assigned_blue.add(index)
            oriented.append(
                orient_segment(predicted_from, predicted_to, first, second)
            )
        actual_from = np.mean([segment[0] for segment in oriented], axis=0)
        actual_to = np.mean([segment[1] for segment in oriented], axis=0)
        collector_page_segments[line["id"]] = (actual_from, actual_to)
        collector_node_observations[line["from"]].append(actual_from)
        collector_node_observations[line["to"]].append(actual_to)

    if len(assigned_blue) != len(blue_polygons):
        raise ValueError("Quedaron polígonos azules sin asociar a un colector")

    node_pdf_points = {}
    for node_id, observations in collector_node_observations.items():
        center = np.mean(observations, axis=0)
        spread = max(float(np.linalg.norm(point - center)) for point in observations)
        if spread > 1.0:
            raise ValueError(
                f"Los colectores no convergen en {node_id}: dispersión {spread:.3f} pt"
            )
        node_pdf_points[node_id] = center

    # Collector polylines and ties often stop at the edge of the magenta chamber
    # symbol. Recover the insertion center from the vector hatch itself.
    for node_id, old_node in old_nodes.items():
        if old_node["elementType"] != "chamber":
            continue
        predicted = apply_affine((old_node["x"], old_node["y"]), old_to_pdf)
        nearby_points = []
        for item in magenta_fills:
            points = np.asarray(item["pts"], dtype=float)
            if float(np.linalg.norm(points.mean(axis=0) - predicted)) <= 7.0:
                nearby_points.extend(points.tolist())
        if not nearby_points:
            raise ValueError(f"No se encontró el símbolo magenta de {node_id}")
        points = np.asarray(nearby_points, dtype=float)
        lower = points.min(axis=0)
        upper = points.max(axis=0)
        diameter = upper - lower
        if not (8.0 <= diameter[0] <= 12.0 and 8.0 <= diameter[1] <= 12.0):
            raise ValueError(
                f"Símbolo magenta ambiguo en {node_id}: "
                f"{diameter[0]:.2f} x {diameter[1]:.2f} pt"
            )
        node_pdf_points[node_id] = (lower + upper) / 2.0

    incident_red = defaultdict(list)
    remaining_red = set(range(len(red_segments)))
    for index, (first, second) in enumerate(red_segments):
        matches = []
        for node_id, point in node_pdf_points.items():
            if old_nodes[node_id]["elementType"] != "chamber":
                continue
            for endpoint_index, endpoint in enumerate((first, second)):
                distance = float(np.linalg.norm(endpoint - point))
                if distance <= 7.0:
                    matches.append((distance, node_id, endpoint_index))
        if matches:
            _, chamber_id, chamber_endpoint = min(matches)
            incident_red[chamber_id].append((index, chamber_endpoint))

    ties_by_chamber = defaultdict(list)
    sump_to_sump_ties = []
    for line in ties:
        from_type = old_nodes[line["from"]]["elementType"]
        to_type = old_nodes[line["to"]]["elementType"]
        if from_type == "chamber":
            ties_by_chamber[line["from"]].append(line)
        elif to_type == "chamber":
            ties_by_chamber[line["to"]].append(line)
        else:
            sump_to_sump_ties.append(line)

    tie_length_differences = []
    for chamber_id, old_group in ties_by_chamber.items():
        segment_group = incident_red.get(chamber_id, [])
        if len(old_group) != len(segment_group):
            raise ValueError(
                f"{chamber_id}: {len(old_group)} tirantes esperados y "
                f"{len(segment_group)} encontrados"
            )
        actual_lengths = []
        for segment_index, chamber_endpoint in segment_group:
            segment = red_segments[segment_index]
            other_endpoint = 1 - chamber_endpoint
            start_utm = apply_affine(segment[chamber_endpoint], pdf_to_utm)
            end_utm = apply_affine(segment[other_endpoint], pdf_to_utm)
            actual_lengths.append(float(np.linalg.norm(end_utm - start_utm)))

        best_permutation = None
        best_cost = math.inf
        for permutation in itertools.permutations(range(len(segment_group))):
            cost = sum(
                abs(float(old_group[index]["meters"]) - actual_lengths[segment_index])
                for index, segment_index in enumerate(permutation)
            )
            if cost < best_cost:
                best_cost = cost
                best_permutation = permutation
        assert best_permutation is not None

        for old_index, group_index in enumerate(best_permutation):
            line = old_group[old_index]
            segment_index, chamber_endpoint = segment_group[group_index]
            segment = red_segments[segment_index]
            sump_endpoint = 1 - chamber_endpoint
            sump_id = line["from"] if line["from"] != chamber_id else line["to"]
            node_pdf_points[sump_id] = segment[sump_endpoint]
            remaining_red.discard(segment_index)
            tie_length_differences.append(
                abs(float(line["meters"]) - actual_lengths[group_index])
            )

    if len(sump_to_sump_ties) != 1 or len(remaining_red) != 1:
        raise ValueError(
            f"Se esperaba un tirante sumidero-sumidero; hay "
            f"{len(sump_to_sump_ties)} definiciones y {len(remaining_red)} segmentos"
        )
    chained_tie = sump_to_sump_ties[0]
    chained_segment = red_segments[next(iter(remaining_red))]
    predicted_from = node_pdf_points.get(
        chained_tie["from"],
        apply_affine(
            (
                old_nodes[chained_tie["from"]]["x"],
                old_nodes[chained_tie["from"]]["y"],
            ),
            old_to_pdf,
        ),
    )
    predicted_to = node_pdf_points.get(
        chained_tie["to"],
        apply_affine(
            (
                old_nodes[chained_tie["to"]]["x"],
                old_nodes[chained_tie["to"]]["y"],
            ),
            old_to_pdf,
        ),
    )
    oriented_chain = orient_segment(predicted_from, predicted_to, *chained_segment)
    for node_id, endpoint in zip(
        (chained_tie["from"], chained_tie["to"]), oriented_chain
    ):
        if node_id in node_pdf_points:
            if float(np.linalg.norm(node_pdf_points[node_id] - endpoint)) > 1.0:
                raise ValueError(
                    "El tirante entre sumideros no coincide con sus extremos"
                )
        else:
            node_pdf_points[node_id] = endpoint
    chained_length = float(
        np.linalg.norm(
            apply_affine(chained_segment[0], pdf_to_utm)
            - apply_affine(chained_segment[1], pdf_to_utm)
        )
    )
    tie_length_differences.append(abs(float(chained_tie["meters"]) - chained_length))

    if set(node_pdf_points) != set(old_nodes):
        missing = sorted(set(old_nodes) - set(node_pdf_points))
        raise ValueError(f"Faltan posiciones PDF para: {', '.join(missing)}")

    collector_length_differences = []
    new_lines = []
    for old_line in network["lines"]:
        if old_line["elementType"] == "collector":
            page_from, page_to = collector_page_segments[old_line["id"]]
            actual_length = float(
                np.linalg.norm(
                    apply_affine(page_from, pdf_to_utm)
                    - apply_affine(page_to, pdf_to_utm)
                )
            )
            collector_length_differences.append(
                abs(float(old_line["meters"]) - actual_length)
            )
        page_from = node_pdf_points[old_line["from"]]
        page_to = node_pdf_points[old_line["to"]]
        display_from = pdf_to_display(page_from)
        display_to = pdf_to_display(page_to)
        line = dict(old_line)
        line["d"] = (
            f"M {display_from[0]:.2f} {display_from[1]:.2f} "
            f"L {display_to[0]:.2f} {display_to[1]:.2f}"
        )
        new_lines.append(line)

    new_nodes = []
    for old_node in network["nodes"]:
        display = pdf_to_display(node_pdf_points[old_node["id"]])
        node = dict(old_node)
        node["x"] = round(float(display[0]), 2)
        node["y"] = round(float(display[1]), 2)
        new_nodes.append(node)

    control_points = []
    combined_controls = boundary_controls | pond_controls
    for number in range(1, 9):
        point_id = f"P{number}"
        display = pdf_to_display(combined_controls[point_id])
        east, north = CONTROL_COORDINATES[point_id]
        control_points.append(
            {
                "id": point_id,
                "x": round(float(display[0]), 2),
                "y": round(float(display[1]), 2),
                "east": east,
                "north": north,
                "feature": "boundary" if number <= 4 else "pond",
            }
        )

    boundary_display = [rounded_point(pdf_to_display(point)) for point in boundary_pdf]
    pond_display = [rounded_point(pdf_to_display(point)) for point in pond_pdf]

    zoom = 19
    web_pixels = np.asarray(
        [web_mercator_pixel(*CONTROL_COORDINATES[f"P{number}"], zoom) for number in range(1, 9)]
    )
    tile_origin_x = int(math.floor(float(web_pixels[:, 0].min()) / 256))
    tile_origin_y = int(math.floor(float(web_pixels[:, 1].min()) / 256))
    local_pixels = web_pixels - np.array([tile_origin_x * 256, tile_origin_y * 256])
    display_controls = np.asarray(
        [[point["x"], point["y"]] for point in control_points], dtype=float
    )
    satellite_transform, satellite_rms = affine_fit(local_pixels, display_controls)

    all_display_points = np.asarray(
        [
            *[[node["x"], node["y"]] for node in new_nodes],
            *[[point["x"], point["y"]] for point in boundary_display],
            *[[point["x"], point["y"]] for point in pond_display],
        ],
        dtype=float,
    )
    view_margin = 85.0
    minimum = all_display_points.min(axis=0) - view_margin
    maximum = all_display_points.max(axis=0) + view_margin

    network["nodes"] = new_nodes
    network["lines"] = new_lines
    network["viewBox"] = {
        "x": round(float(minimum[0]), 2),
        "y": round(float(minimum[1]), 2),
        "width": round(float(maximum[0] - minimum[0]), 2),
        "height": round(float(maximum[1] - minimum[1]), 2),
    }
    network["georeference"] = {
        "crs": "EPSG:32717",
        "orientationVersion": 3,
        "orientation": "north-up",
        "handednessValidated": True,
        "controlSource": "Exportacion_AALL_18Agosto-Model.pdf",
        "controlPointCount": 8,
        "controlRmsMeters": round(control_rms, 6),
        "pdfToUtmTransform": {
            "a": round(float(pdf_to_utm[0, 0]), 10),
            "b": round(float(pdf_to_utm[0, 1]), 10),
            "c": round(float(pdf_to_utm[1, 0]), 10),
            "d": round(float(pdf_to_utm[1, 1]), 10),
            "e": round(float(pdf_to_utm[2, 0]), 6),
            "f": round(float(pdf_to_utm[2, 1]), 6),
        },
        "displayOrigin": {
            "east": round(display_origin_east, 4),
            "north": round(display_origin_north, 4),
            "unitsPerMeter": DISPLAY_UNITS_PER_METER,
        },
    }
    network["siteGeometry"] = {
        "crs": "EPSG:32717",
        "sourceFile": "Exportacion_AALL_18Agosto-Model.pdf",
        "boundary": {
            "id": "LINDERO_COSTANERA",
            "name": "Lindero / perímetro",
            "points": boundary_display,
        },
        "areas": [
            {
                "id": "ESTANQUE_COSTANERA",
                "name": "Estanque",
                "category": "boundary",
                "points": pond_display,
            }
        ],
        "controlPoints": control_points,
        "satellite": {
            "zoom": zoom,
            "tileOriginX": tile_origin_x,
            "tileOriginY": tile_origin_y,
            "transform": {
                "a": round(float(satellite_transform[0, 0]), 10),
                "b": round(float(satellite_transform[0, 1]), 10),
                "c": round(float(satellite_transform[1, 0]), 10),
                "d": round(float(satellite_transform[1, 1]), 10),
                "e": round(float(satellite_transform[2, 0]), 8),
                "f": round(float(satellite_transform[2, 1]), 8),
            },
            "rmsDisplayUnits": round(satellite_rms, 6),
        },
    }

    project["revision"] = 3
    project["note"] = (
        "Modelo maduro extraído del plano Exportacion_AALL_18Agosto-Model.pdf, "
        "con lindero, estanque y georreferenciación de ocho puntos"
    )
    project["metadata"] = {
        "sourceFile": "Exportacion_AALL_18Agosto-Model.pdf",
        "sourceCreatedAt": "2026-08-19T04:52:14-05:00",
        "collectorCount": len(collectors),
        "tieCount": len(ties),
        "chamberCount": sum(
            node["elementType"] == "chamber" for node in new_nodes
        ),
        "sumpCount": sum(node["elementType"] == "sump" for node in new_nodes),
        "dischargeCount": sum(
            node["elementType"] == "discharge" for node in new_nodes
        ),
        "collectorMeters": round(sum(float(line["meters"]) for line in collectors), 2),
        "tieMeters": round(sum(float(line["meters"]) for line in ties), 2),
        "controlPointCount": 8,
        "controlRmsMeters": round(control_rms, 6),
        "hasPond": True,
    }

    payload = json.dumps(project, ensure_ascii=False, indent=2)
    output = (
        '(function () {\n'
        '  "use strict";\n\n'
        f"  const project = {payload};\n"
        '  if (!project.snapshot.network.definition && typeof NETWORK_DEFINITION !== "undefined") {\n'
        '    project.snapshot.network.definition = JSON.parse(JSON.stringify(NETWORK_DEFINITION));\n'
        '  }\n'
        '  window.RED_NETWORK_BUILTIN_PROJECTS = Object.freeze([project]);\n'
        '})();\n'
    )
    output_path.write_text(output, encoding="utf-8")

    return {
        "source": str(source_pdf),
        "output": str(output_path),
        "inventory": project["metadata"],
        "outerControlRmsMeters": outer_rms,
        "controlRmsMeters": control_rms,
        "satelliteRmsDisplayUnits": satellite_rms,
        "maxCollectorLengthDifferenceMeters": max(collector_length_differences),
        "maxTieLengthDifferenceMeters": max(tie_length_differences),
        "viewBox": network["viewBox"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--project", type=Path, default=Path("costanera-acacias-aall.js"))
    parser.add_argument("--output", type=Path, default=Path("costanera-acacias-aall.js"))
    args = parser.parse_args()
    result = generate(args.pdf, args.project, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
