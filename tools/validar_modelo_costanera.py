from __future__ import annotations

import json
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_project() -> dict:
    source = (ROOT / "costanera-acacias-aall.js").read_text(encoding="utf-8")
    match = re.search(r"const project = (\{.*?\n\});\n  if", source, re.S)
    if not match:
        raise AssertionError("No se encontró el proyecto incorporado")
    return json.loads(match.group(1))


def main() -> None:
    project = load_project()
    assert project["revision"] == 3
    network = project["snapshot"]["network"]
    nodes = {node["id"]: node for node in network["nodes"]}
    lines = {line["id"]: line for line in network["lines"]}
    assert len(nodes) == 103
    assert len(lines) == 98
    assert len(nodes) == len(network["nodes"])
    assert len(lines) == len(network["lines"])

    inventory = {
        "chambers": sum(node["elementType"] == "chamber" for node in nodes.values()),
        "sumps": sum(node["elementType"] == "sump" for node in nodes.values()),
        "discharges": sum(node["elementType"] == "discharge" for node in nodes.values()),
        "collectors": sum(line["elementType"] == "collector" for line in lines.values()),
        "ties": sum(line["elementType"] == "tie" for line in lines.values()),
    }
    assert inventory == {
        "chambers": 32,
        "sumps": 67,
        "discharges": 4,
        "collectors": 31,
        "ties": 67,
    }

    discharge_connections = {node_id: 0 for node_id, node in nodes.items() if node["elementType"] == "discharge"}
    max_endpoint_error = 0.0
    path_pattern = re.compile(
        r"^M ([+-]?\d+(?:\.\d+)?) ([+-]?\d+(?:\.\d+)?) "
        r"L ([+-]?\d+(?:\.\d+)?) ([+-]?\d+(?:\.\d+)?)$"
    )
    for line in lines.values():
        assert line["from"] in nodes and line["to"] in nodes
        assert float(line["meters"]) > 0
        match = path_pattern.match(line["d"])
        assert match, line["id"]
        x0, y0, x1, y1 = map(float, match.groups())
        source = nodes[line["from"]]
        target = nodes[line["to"]]
        max_endpoint_error = max(
            max_endpoint_error,
            math.hypot(x0 - source["x"], y0 - source["y"]),
            math.hypot(x1 - target["x"], y1 - target["y"]),
        )
        endpoint_types = {source["elementType"], target["elementType"]}
        if line["elementType"] == "collector":
            assert endpoint_types in ({"chamber"}, {"chamber", "discharge"})
            for node_id in (line["from"], line["to"]):
                if node_id in discharge_connections:
                    discharge_connections[node_id] += 1
                    assert line["flowTo"] == node_id
        else:
            assert endpoint_types in ({"chamber", "sump"}, {"sump"})
    assert max_endpoint_error < 0.02
    assert set(discharge_connections.values()) == {1}

    site = network["siteGeometry"]
    assert len(site["boundary"]["points"]) == 4
    assert len(site["areas"]) == 1
    assert site["areas"][0]["name"] == "Estanque"
    assert len(site["areas"][0]["points"]) == 17
    controls = site["controlPoints"]
    assert [point["id"] for point in controls] == [f"P{number}" for number in range(1, 9)]
    assert [point["feature"] for point in controls[:4]] == ["boundary"] * 4
    assert [point["feature"] for point in controls[4:]] == ["pond"] * 4

    origin = network["georeference"]["displayOrigin"]
    max_control_display_error = 0.0
    for point in controls:
        expected_x = (point["east"] - origin["east"]) * origin["unitsPerMeter"]
        expected_y = (origin["north"] - point["north"]) * origin["unitsPerMeter"]
        max_control_display_error = max(
            max_control_display_error,
            math.hypot(point["x"] - expected_x, point["y"] - expected_y),
        )
    assert max_control_display_error < 0.08
    assert network["georeference"]["controlRmsMeters"] < 0.01
    satellite = site["satellite"]
    transform = satellite["transform"]
    determinant = transform["a"] * transform["d"] - transform["b"] * transform["c"]
    assert determinant > 0
    assert satellite["rmsDisplayUnits"] < 0.1

    index = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "Lindero / estanque" in index
    assert "site-area-outline" in index
    assert "RED_NETWORK_SITE_GEOMETRY" in index
    version_manager = (ROOT / "version-manager.js").read_text(encoding="utf-8")
    assert "siteGeometry: clone(globalThis.RED_NETWORK_SITE_GEOMETRY || null)" in version_manager

    print(
        json.dumps(
            {
                "ok": True,
                "revision": project["revision"],
                "inventory": inventory,
                "maxLineEndpointError": round(max_endpoint_error, 6),
                "maxControlDisplayError": round(max_control_display_error, 6),
                "controlRmsMeters": network["georeference"]["controlRmsMeters"],
                "satelliteRmsDisplayUnits": satellite["rmsDisplayUnits"],
                "areas": [area["name"] for area in site["areas"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
