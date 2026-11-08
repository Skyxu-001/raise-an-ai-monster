"""Skeleton scoring and body part detection, plain numpy. All coordinates are glTF: +Y is up, +Z is where the face points (all TRELLIS models are like this).

score: picks the best skeleton when the same monster runs with several seeds (lower is better).
roles: which bone chains are head / tail / legs / arms / wings / other (tentacles and such), and where the mouth is, for animation in the front end.
"""
import numpy as np


def seg_dist(v, joints, parents):
    """Distance from each vertex to its nearest bone (the segment from parent joint to child joint)."""
    a = np.array([joints[p] if p >= 0 else joints[j] for j, p in enumerate(parents)])
    b = np.asarray(joints)
    ab = b - a
    best = np.full(len(v), np.inf)
    for i in range(len(a)):
        L = ab[i] @ ab[i]
        t = np.zeros(len(v)) if L < 1e-12 else np.clip((v - a[i]) @ ab[i] / L, 0, 1)
        best = np.minimum(best, np.linalg.norm(v - (a[i] + t[:, None] * ab[i]), axis=1))
    return best


def score(v, joints, parents, owner):
    """Share of vertices far from every bone (high when a tail has no bones) plus a small penalty for useless joints (zero length, or driving no vertices)."""
    h = v[:, 1].max() - v[:, 1].min()
    far = float((seg_dist(v, joints, parents) > 0.2 * h).mean())
    bone_len = np.array([np.linalg.norm(np.subtract(joints[j], joints[p])) if p >= 0 else 1.0 for j, p in enumerate(parents)])
    dead = int(((bone_len < 0.01 * h) | (np.bincount(owner, minlength=len(parents)) == 0)).sum())
    return far + 0.004 * dead, {"far": round(far, 4), "dead_joints": dead}


def roles(v, joints, parents, owner):
    """Find the trunk first (root → head). Every other leaf, traced up until it hits the trunk, is one limb, sorted by where it grows: to the ground is a leg, backward is a tail,
    to the sides is an arm or wing (growing upward counts as a wing). Leaves that branch off the same arm, like fingers, merge into one limb. If all leaves are in the lower half (a jellyfish) there is no head,
    and all limbs count as other. How to use these on serpent or floating bodies is up to the front end, by body plan."""
    joints = np.asarray(joints)
    J = len(parents)
    lo, hi = v.min(0), v.max(0)
    size = np.maximum(hi - lo, 1e-9)
    center = (lo + hi) / 2
    kids = [[] for _ in range(J)]
    for j, p in enumerate(parents):
        if p >= 0:
            kids[p].append(j)
    leaves = [j for j in range(J) if not kids[j]]

    def rel(j):
        return (joints[j] - lo) / size

    def path(j):
        out = [j]
        while parents[out[-1]] >= 0:
            out.append(parents[out[-1]])
        return out[::-1]

    heads = [l for l in leaves if rel(l)[1] > 0.5 and abs(joints[l][0] - center[0]) < 0.25 * size[0]]
    head_leaf = max(heads, key=lambda l: rel(l)[1] + rel(l)[2]) if heads else None
    trunk = path(head_leaf) if head_leaf is not None else [j for j in range(J) if parents[j] < 0][:1]
    on_trunk = set(trunk)

    limbs = {}  # first joint of a limb → all joints on that limb
    for l in leaves:
        if l in on_trunk:
            continue
        p = path(l)
        k = max(i for i, j in enumerate(p) if j in on_trunk)
        limb = p[k + 1:]
        limbs.setdefault(limb[0], set()).update(limb)

    out = {"head_path": trunk if head_leaf is not None else [], "tail": [], "legs": [], "arms": [], "wings": [], "other": []}
    for first, js in limbs.items():
        js = sorted(js, key=lambda j: len(path(j)))  # ordered from the body end to the tip
        tip = js[-1] if len(js) == 1 else max(js, key=lambda j: np.linalg.norm(joints[j] - joints[first]))
        d = (joints[tip] - joints[parents[first]]) / size
        if head_leaf is None:
            kind = "other"
        elif rel(tip)[1] < 0.15:
            kind = "legs"
        elif d[2] < -0.15 and rel(parents[first])[1] < 0.5:  # grows out of the lower body and backward (raised or off-center tails count too)
            kind = "tail"
        elif abs(d[0]) > 0.2:
            kind = "wings" if d[1] > 0.1 else "arms"
        else:
            kind = "other"
        out[kind].append(js)
    if out["tail"]:  # keep only the longest as the tail; put the rest back in other
        out["tail"].sort(key=len, reverse=True)
        out["other"] += out["tail"][1:]
        out["tail"] = out["tail"][0]

    # Mouth: the frontmost vertex driven by the last three trunk joints (a joint may be empty and drive no vertices, hence three); with no head, use the point straight in front of the body
    mouth = None
    if head_leaf is not None:
        idx = np.where(np.isin(owner, trunk[-3:]))[0]
        if len(idx):
            k = idx[np.argmax(v[idx, 2])]
            bone = int(owner[k])
            mouth = {"bone": bone, "offset": (v[k] - joints[bone]).round(4).tolist(), "pos": v[k].round(4).tolist()}
    if mouth is None:
        m = np.array([center[0], center[1], hi[2]])
        bone = int(np.argmin(np.linalg.norm(joints - center, axis=1)))
        mouth = {"bone": bone, "offset": (m - joints[bone]).round(4).tolist(), "pos": m.round(4).tolist()}
    out["mouth"] = mouth
    out["bbox"] = [lo.round(4).tolist(), hi.round(4).tolist()]
    return out
