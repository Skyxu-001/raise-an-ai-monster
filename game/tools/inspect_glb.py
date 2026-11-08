# /// script
# dependencies = ["trimesh", "numpy", "matplotlib", "pillow", "scipy"]
# ///
"""Inspect a GLB: mesh count, face count, whether it has textures. Also renders a preview from three angles (colored by sampling the texture at vertex UVs).

uv run inspect_glb.py model.glb preview.png
"""
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import trimesh
from mpl_toolkits.mplot3d.art3d import Poly3DCollection


def vertex_colors(geom):
    vis = geom.visual
    mat = getattr(vis, "material", None)
    tex = getattr(mat, "baseColorTexture", None) if mat is not None else None
    uv = getattr(vis, "uv", None)
    if tex is None or uv is None:
        return None, None
    img = np.asarray(tex.convert("RGB"), dtype=np.float32) / 255.0
    h, w = img.shape[:2]
    px = np.clip((uv[:, 0] % 1.0) * (w - 1), 0, w - 1).astype(int)
    py = np.clip((1 - uv[:, 1] % 1.0) * (h - 1), 0, h - 1).astype(int)
    return img[py, px], tex.size


def main(path, out):
    scene = trimesh.load(path, force="scene")
    report = {"geometries": len(scene.geometry), "bounds": scene.bounds.round(3).tolist()}
    meshes = []
    for name, g in scene.geometry.items():
        cols, tex_size = vertex_colors(g)
        mat = getattr(g.visual, "material", None)
        report[name] = {
            "vertices": len(g.vertices), "faces": len(g.faces),
            "has_uv": getattr(g.visual, "uv", None) is not None,
            "baseColorTexture": tex_size,
            "normalTexture": getattr(getattr(mat, "normalTexture", None), "size", None),
            "metallicRoughnessTexture": getattr(getattr(mat, "metallicRoughnessTexture", None), "size", None),
            "occlusionTexture": getattr(getattr(mat, "occlusionTexture", None), "size", None),
        }
        meshes.append((g, cols))
    print(report)

    fig = plt.figure(figsize=(15, 5), dpi=110)
    for i, (elev, azim) in enumerate([(10, -90), (10, 0), (10, -45)]):
        ax = fig.add_subplot(1, 3, i + 1, projection="3d")
        for g, cols in meshes:
            v, f = g.vertices, g.faces
            fc = cols[f].mean(axis=1) if cols is not None else (0.7, 0.7, 0.7)
            # simple directional shading so the shape shows
            n = g.face_normals
            shade = 0.55 + 0.45 * np.clip(n @ np.array([0.3, 0.5, 0.8]) / np.linalg.norm([0.3, 0.5, 0.8]), 0, 1)
            fc = np.clip(np.asarray(fc) * shade[:, None], 0, 1) if cols is not None else fc
            # glTF is Y up, matplotlib is Z up: (x, y, z) → (x, -z, y)
            vv = v[:, [0, 2, 1]] * np.array([1, -1, 1])
            ax.add_collection3d(Poly3DCollection(vv[f], facecolors=fc, linewidths=0))
        allv = np.concatenate([g.vertices for g, _ in meshes])[:, [0, 2, 1]] * np.array([1, -1, 1])
        c, r = allv.mean(axis=0), (allv.max(axis=0) - allv.min(axis=0)).max() / 2
        ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r); ax.set_zlim(c[2] - r, c[2] + r)
        ax.view_init(elev=elev, azim=azim); ax.set_axis_off()
    plt.tight_layout()
    plt.savefig(out)
    print("preview ->", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
