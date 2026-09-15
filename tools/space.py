#!/usr/bin/env python3
"""
NE space -> the original space the levels and models were authored in.

Climax's converter mirrored everything on the way to the GameCube. Every NE
placement matrix is the Serious Editor rotation with a mirror on each side:

    M_NE = S_z . R_orig . S_x        S_z = diag(1, 1, -1), S_x = diag(-1, 1, 1)

- **The world is mirrored in Z.** The exported level meshes came out mirrored
  against the game; that was checked by eye.
- **Every model's local space is mirrored in X.**
  - The level items match their SE1 modeller sources in dev/ vertex for vertex
    once X is negated: dynamite 475/476, the shield 34/34, the Neptune statue
    729/731.
  - The character sources show the same.
- **The camera markers store the editor's own angles.** Their matrices equal
  MakeRotationMatrix(180 - h, p, -b), which is S_z . R(h, p, b) . S_x, on
  443 of 443 placements (docs/world-conversion.md, "Axes").
- **A brush's local space is mirrored in Z instead**, like the world:
  M_NE = S_z . R_orig . S_z for the brush nodes at image+0x48 (movers,
  destroyables, extra WorldBases). A mover's key matrices are entity
  placements like the camera markers, and key 0 is the brush's own placement:
  S_z . R . S_x of key 0 equals S_z . R . S_z of the brush on 953 of 958
  movers, and S_z . R . S_x of the brush differs from it by a half turn.

Every exporter goes through these functions, so OBJ files and SE1
descriptions come out in the original space:

    world point / direction   (x, y, z) -> (x, y, -z)
    model-local point         (x, y, z) -> (-x, y, z)
    placement rotation        R -> S_z . R . S_x
    brush node rotation       R -> S_z . R . S_z
    placement translation     as a world point

A mirror reverses handedness, so each exported face has its vertex order
reversed to keep facing the same way. The raw decodes (tools/entity.py json,
the NE-side analysis) stay in NE space.
"""

from __future__ import annotations

_SZ = (1.0, 1.0, -1.0)
_SX = (-1.0, 1.0, 1.0)


def world(v):
    """A world-space point or direction vector."""
    return (v[0], v[1], -v[2])


def local(v):
    """A point or vector in a model's local space."""
    return (-v[0], v[1], v[2])


def rotation(rows):
    """A placement's 3x3 rows (NE) -> the original rotation, S_z . R . S_x."""
    return [[_SZ[i] * rows[i][j] * _SX[j] for j in range(3)] for i in range(3)]


def brush_rotation(rows):
    """A brush node's 3x3 rows (NE) -> the original rotation, S_z . R . S_z."""
    return [[_SZ[i] * rows[i][j] * _SZ[j] for j in range(3)] for i in range(3)]


def face(ids):
    """Vertex order of one face after a mirror."""
    return list(ids)[::-1]


def _vec3(v):
    return isinstance(v, (list, tuple)) and len(v) == 3 and all(
        isinstance(c, (int, float)) for c in v)


def _mat3(v):
    return isinstance(v, (list, tuple)) and len(v) == 3 and all(_vec3(r) for r in v)


# Keys of a tools/world.py description and how each converts. Anything else is
# recursed into unchanged. Angles are never under these keys: the Bouncer's
# heading and pitch stay raw under "ne", and its push is a world vector.
WORLD_KEYS = {"position", "direction", "translation"}
ROTATION_KEYS = {"rows", "rotation", "placement_rot"}
BRUSH_ROTATION_KEYS = {"brush_rows"}
LOCAL_KEYS = {"local_vector", "second_vector"}


def to_original(o):
    """Convert a world.py description, recursively, keyed by field name."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k in WORLD_KEYS and _vec3(v):
                out[k] = list(world(v))
            elif k in ROTATION_KEYS and _mat3(v):
                out[k] = rotation(v)
            elif k in BRUSH_ROTATION_KEYS and _mat3(v):
                out[k] = brush_rotation(v)
            elif k in LOCAL_KEYS and _vec3(v):
                out[k] = list(local(v))
            elif k == "vertices" and isinstance(v, list) and all(_vec3(p) for p in v):
                out[k] = [list(world(p)) for p in v]
            elif k == "faces" and isinstance(v, list) and all(isinstance(f, (list, tuple)) for f in v):
                out[k] = [face(f) for f in v]
            elif k == "faces" and isinstance(v, list) and all(isinstance(f, dict) and "verts" in f for f in v):
                # a region cell's faces (tools/bsp.py): the vertex order and
                # the plane {a, b}, a.p + b >= 0 inside, both mirrored
                out[k] = [dict(f, verts=face(f["verts"]),
                               **({"plane": list(world(f["plane"][:3])) + [f["plane"][3]]}
                                  if "plane" in f else {}))
                          for f in v]
            else:
                out[k] = to_original(v)
        return out
    if isinstance(o, list):
        return [to_original(x) for x in o]
    return o
