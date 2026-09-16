"""Unit tests for named-Scene -> GLB export/round-trip (issue #7).

No Docker required — builds tiny trimesh primitives directly (standing in
for the STL an isolated openscad render would produce) and asserts the
GLB round-trip preserves ``registry_name`` keys as glTF node/mesh names,
exactly the shape ``ModelViewer.tsx``'s ``resolvePointPick`` reads
via ``mesh.name`` in three.js's ``GLTFLoader``.

The full Docker-driven orchestration (:func:`build_registry_glb` end to
end against the real openscad image) is covered by the ``slow`` layer in
``tests/slow/test_module_registry_docker.py``.
"""

from __future__ import annotations

import trimesh

from d33d.module_registry import RegistryBuildResult


def _scene_glb_bytes(names_and_meshes: dict[str, trimesh.Trimesh]) -> bytes:
    scene = trimesh.Scene(names_and_meshes)
    result = scene.export(file_type="glb")
    assert isinstance(result, (bytes, bytearray))
    return bytes(result)


def test_named_scene_glb_round_trip_preserves_names(tmp_path) -> None:
    base = trimesh.creation.box(extents=(20, 20, 20))
    post = trimesh.creation.cylinder(radius=5, height=30)
    cap = trimesh.creation.icosphere(radius=8)

    glb_bytes = _scene_glb_bytes({"base": base, "post": post, "cap": cap})

    out_path = tmp_path / "combined.glb"
    out_path.write_bytes(glb_bytes)

    reloaded = trimesh.load(str(out_path))
    assert isinstance(reloaded, trimesh.Scene)
    assert set(reloaded.geometry.keys()) == {"base", "post", "cap"}


def test_single_module_scene_round_trip() -> None:
    only = trimesh.creation.box(extents=(1, 1, 1))
    glb_bytes = _scene_glb_bytes({"widget": only})

    import io

    reloaded = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")
    assert isinstance(reloaded, trimesh.Scene)
    assert set(reloaded.geometry.keys()) == {"widget"}


def test_duplicate_module_ordinal_names_survive_round_trip() -> None:
    """``leg`` / ``leg_2`` / ``leg_3`` (the parser's disambiguation
    convention for repeated calls to the same module) must all survive
    the GLB round-trip as distinct, independently-named entries."""
    leg = trimesh.creation.cylinder(radius=3, height=40)
    leg2 = trimesh.creation.cylinder(radius=3, height=40)
    leg3 = trimesh.creation.cylinder(radius=3, height=40)

    glb_bytes = _scene_glb_bytes({"leg": leg, "leg_2": leg2, "leg_3": leg3})

    import io

    reloaded = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")
    assert set(reloaded.geometry.keys()) == {"leg", "leg_2", "leg_3"}


def test_registry_build_result_is_frozen_dataclass_with_expected_fields() -> None:
    result = RegistryBuildResult(
        glb_bytes=b"stub", registry_names=("base",), failures=()
    )
    assert result.glb_bytes == b"stub"
    assert result.registry_names == ("base",)
    assert result.failures == ()


def test_raw_gltf_json_node_and_mesh_names_match_registry_keys() -> None:
    """Bypass trimesh's loader entirely and parse the GLB's raw glTF JSON
    chunk directly \u2014 the same verification the empirical spike used to
    confirm three.js's GLTFLoader will read the same names via
    ``nodes[].name`` / ``meshes[].name``."""
    import json
    import struct

    base = trimesh.creation.box(extents=(20, 20, 20))
    post = trimesh.creation.cylinder(radius=5, height=30)
    glb_bytes = _scene_glb_bytes({"base": base, "post": post})

    # GLB container: 12-byte header, then a JSON chunk (chunk 0).
    assert glb_bytes[:4] == b"glTF"
    json_chunk_length = struct.unpack_from("<I", glb_bytes, 12)[0]
    json_chunk_type = glb_bytes[16:20]
    assert json_chunk_type == b"JSON"
    json_bytes = glb_bytes[20 : 20 + json_chunk_length]
    doc = json.loads(json_bytes)

    node_names = {n.get("name") for n in doc.get("nodes", [])}
    mesh_names = {m.get("name") for m in doc.get("meshes", [])}
    assert {"base", "post"} <= node_names
    assert {"base", "post"} <= mesh_names
