#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skylanders SuperChargers Racing (Wii) - Rebuilt Static Vehicle Importer
=================================================================

Experimental importer distilled from the working vehicle tests:
- FireCar_SC
- KoopaPlane_SC
- LifeCopter_SC

It replaces a STATIC "Model Fix" mesh with a custom GLB, rebuilds all LODs,
keeps the target GX/VAT choice, packs custom materials into one texture atlas,
re-encodes the atlas as Wii RGB5A3 (fmt=2), and repairs PKZ absolute asset references.

Validated target layout so far:
    split geometry
    0x1771 positions/normals
    0x1770 skin table
    0x1388 stride 16: color + UV + skin mirror
    0x1389 GX display list
    slots: pos, nrm, clr, uv0
    VAT3 / opcode 0x9B on the tested vehicle Model Fixes

IMPORTANT LIMITATIONS
---------------------
- STATIC Model Fixes only. This is not the character/skinned importer.
- Target must use the validated stride-16 layout above.
- Custom physical vertex count must be < 65536 (u16 GX indices).
- No automatic mesh decimation yet.
- Atlas packing is intentionally simple. Complex UV wrapping or unusual
  materials may need manual cleanup.
- All custom geometry is rigidly bound to one dominant target joint.
- All target LODs receive the same custom geometry.

STANDALONE BUILD: helper modules are embedded in this single file.

Example:
    python skylanders_static_vehicle_importer.py ^
        --pkz 3228_LifeCopter_Mod_SC.pkz ^
        --target-glb 3228_LifeCopter_Mod_SC__LifeCopter_SC.glb ^
        --custom Yoshi.glb ^
        --model LifeCopter_SC ^
        --out 3228_LifeCopter_Mod_SC_YOSHI.pkz
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# STANDALONE embedded helpers
# ---------------------------------------------------------------------------
# This build embeds sklib.py, sk_geo.py and goliath_pkz.py, and includes the
# small glTF reader needed by this importer. No sibling helper .py files needed.
import sys as _sys, types as _types, json as _json, struct as _struct, math as _math

_SKLIB_SOURCE = '#!/usr/bin/env python3\n"""sklib.py - Skylanders SuperChargers Racing (Wii) shared library.\n\nChunk container, asset enumeration, texture registry + GX decode, materials,\nskeletons, and the glTF (.glb) writer.  See FORMAT_GUIDE_SSCR_WII.md.\n\nContainer: every .pkz (and Streams2.dat) is an UNCOMPRESSED chunk file.\n16-byte big-endian chunk headers:\n    u32 idword   -- 0x80000000 always set; chunk id = idword & 0x7fffffff\n    u16 version  -- per-id payload version\n    u16 flags    -- bit0 = payload is a list of child chunks\n    u32 reserved -- 0\n    u32 size     -- payload byte length\n"""\nimport struct, os, re, json\nimport numpy as np\n\n# ---------------- chunk walking ----------------\n\ndef header(buf, off):\n    idw, ver, flags, w2, size = struct.unpack_from(">IHHII", buf, off)\n    return idw & 0x7FFFFFFF, ver, flags, size\n\ndef children(buf, off, size):\n    """Children of the container chunk whose header is at `off`."""\n    out = []\n    p, end = off + 16, off + 16 + size\n    while p + 16 <= end:\n        idw, ver, flags, w2, csize = struct.unpack_from(">IHHII", buf, p)\n        if not (idw & 0x80000000) or p + 16 + csize > end:\n            break\n        out.append((idw & 0x7FFFFFFF, p, csize, flags, ver))\n        p += 16 + csize\n    return out\n\ndef top(buf):\n    """Top-level chunks (children of the root 0x0001)."""\n    cid, ver, flags, size = header(buf, 0)\n    return children(buf, 0, size)\n\ndef find_child(buf, kids, want):\n    return next((k for k in kids if k[0] == want), None)\n\n# ---------------- 0x138E asset headers ----------------\n\ndef asset_header(buf, off138e):\n    """0x138E payload -> (hash, type, name). Name is a 64-byte field at +28."""\n    p = off138e + 16\n    h, atype = struct.unpack_from(">II", buf, p)\n    name = buf[p + 28:p + 28 + 64].split(b"\\0")[0].decode("latin1")\n    return h, atype, name\n\n# asset types seen: 2=skeleton 3=model 4=texture 5=animation 10=soundbank-events\n# 23=anim events 257/258=loader/package nodes\ndef assets(buf):\n    """Top-level 0x0026 assets -> [(name, type, hash, off, size, kids)]."""\n    out = []\n    for cid, off, size, flags, ver in top(buf):\n        if cid != 0x26:\n            continue\n        kids = children(buf, off, size)\n        if not kids or kids[0][0] != 0x138E:\n            continue\n        h, atype, name = asset_header(buf, kids[0][1])\n        out.append((name, atype, h, off, size, kids))\n    return out\n\n# ---------------- texture registry (0x0009) ----------------\n\ndef texture_registry(buf):\n    """0x0009 > 0x138D per texture > [0x138E, 0x0191 > 0x0197].\n    0x0197: u32 HEIGHT, WIDTH, mips, fmt, colourBytes, ...  (height first!)\n    -> {\'byname\': lname->info, \'byhash\': hash->info}"""\n    byname, byhash = {}, {}\n    for cid, off, size, flags, ver in top(buf):\n        if cid != 0x0009:\n            continue\n        for c1, o1, s1, f1, v1 in children(buf, off, size):\n            if c1 != 0x138D:\n                continue\n            sub = children(buf, o1, s1)\n            e138e = find_child(buf, sub, 0x138E)\n            e191 = find_child(buf, sub, 0x0191)\n            if not e138e or not e191:\n                continue\n            h, atype, nm = asset_header(buf, e138e[1])\n            e197 = find_child(buf, children(buf, e191[1], e191[2]), 0x0197)\n            if not e197:\n                continue\n            u = struct.unpack_from(">5I", buf, e197[1] + 16)\n            info = {"name": nm, "hash": h, "h": u[0], "w": u[1],\n                    "mips": u[2], "fmt": u[3], "csize": u[4]}\n            byname[nm.lower()] = info\n            byhash[h] = info\n    return {"byname": byname, "byhash": byhash}\n\ndef texture_payloads(buf):\n    """type-4 0x0026 assets -> {hash: (name, payload_off, payload_size)}."""\n    out = {}\n    for name, atype, h, off, size, kids in assets(buf):\n        if atype != 4:\n            continue\n        k195 = find_child(buf, kids, 0x0195)\n        if k195:\n            out[h] = (name, k195[1] + 16, k195[2])\n    return out\n\n# ---------------- GX texture decoding (formats identical to other GX games) --------\n\ndef _detile(blocks, w, h, tw, th):\n    bw, bh = -(-w // tw), -(-h // th)\n    img = blocks.reshape(bh, bw, th, tw, -1).transpose(0, 2, 1, 3, 4)\n    img = img.reshape(bh * th, bw * tw, blocks.shape[-1])\n    return img[:h, :w]\n\ndef _rgb565(c):\n    r = ((c >> 11) & 31).astype(np.uint16)\n    g = ((c >> 5) & 63).astype(np.uint16)\n    b = (c & 31).astype(np.uint16)\n    return np.stack([(r * 255 // 31), (g * 255 // 63), (b * 255 // 31)], -1).astype(np.uint8)\n\ndef dec_cmpr(buf, w, h):\n    """GX CMPR: 8x8 tiles of 2x2 BE DXT1 sub-blocks, texel 0 in the high bits."""\n    bw, bh = -(-w // 8), -(-h // 8)\n    n = bw * bh * 4\n    a = np.frombuffer(buf[:n * 8], np.uint8).reshape(n, 8)\n    c0 = (a[:, 0].astype(np.uint16) << 8) | a[:, 1]\n    c1 = (a[:, 2].astype(np.uint16) << 8) | a[:, 3]\n    p0, p1 = _rgb565(c0).astype(np.int16), _rgb565(c1).astype(np.int16)\n    opaque = (c0 > c1)\n    pal = np.zeros((n, 4, 4), np.uint8)\n    pal[:, 0, :3] = p0; pal[:, 0, 3] = 255\n    pal[:, 1, :3] = p1; pal[:, 1, 3] = 255\n    p2 = np.where(opaque[:, None], (2 * p0 + p1) // 3, (p0 + p1) // 2)\n    p3 = np.where(opaque[:, None], (p0 + 2 * p1) // 3, 0)\n    pal[:, 2, :3] = p2.astype(np.uint8); pal[:, 2, 3] = 255\n    pal[:, 3, :3] = p3.astype(np.uint8)\n    pal[:, 3, 3] = np.where(opaque, 255, 0).astype(np.uint8)\n    idx = a[:, 4:8]\n    sel = np.stack([(idx >> 6) & 3, (idx >> 4) & 3, (idx >> 2) & 3, idx & 3], -1)\n    texel = pal[np.arange(n)[:, None, None], sel]\n    texel = texel.reshape(bh * bw, 2, 2, 4, 4, 4).transpose(0, 1, 3, 2, 4, 5)\n    tiles = texel.reshape(bh * bw, 8, 8, 4)\n    return _detile(tiles, w, h, 8, 8)\n\ndef dec_i8(buf, w, h):\n    bw, bh = -(-w // 8), -(-h // 4)\n    a = np.frombuffer(buf[:bw * bh * 32], np.uint8).reshape(bw * bh, 4, 8, 1)\n    return _detile(a, w, h, 8, 4)[:, :, 0]\n\ndef dec_rgb5a3(buf, w, h):\n    bw, bh = -(-w // 4), -(-h // 4)\n    a = np.frombuffer(buf[:bw * bh * 32], ">u2").reshape(bw * bh, 4, 4)\n    c = a.reshape(-1)\n    out = np.zeros((c.size, 4), np.uint8)\n    m = (c & 0x8000) != 0\n    out[m, 0] = ((c[m] >> 10) & 31) * 255 // 31\n    out[m, 1] = ((c[m] >> 5) & 31) * 255 // 31\n    out[m, 2] = (c[m] & 31) * 255 // 31\n    out[m, 3] = 255\n    out[~m, 0] = ((c[~m] >> 8) & 15) * 17\n    out[~m, 1] = ((c[~m] >> 4) & 15) * 17\n    out[~m, 2] = (c[~m] & 15) * 17\n    out[~m, 3] = ((c[~m] >> 12) & 7) * 255 // 7\n    return _detile(out.reshape(bw * bh, 4, 4, 4), w, h, 4, 4)\n\ndef dec_ia8(buf, w, h):\n    bw, bh = -(-w // 4), -(-h // 4)\n    a = np.frombuffer(buf[:bw * bh * 32], np.uint8).reshape(bw * bh, 4, 4, 2)\n    out = np.zeros((bw * bh, 4, 4, 4), np.uint8)\n    out[..., 0] = out[..., 1] = out[..., 2] = a[..., 1]\n    out[..., 3] = a[..., 0]\n    return _detile(out.reshape(bw * bh, 4, 4, 4), w, h, 4, 4)\n\ndef dec_rgba8(buf, w, h):\n    bw, bh = -(-w // 4), -(-h // 4)\n    a = np.frombuffer(buf[:bw * bh * 64], np.uint8).reshape(bw * bh, 2, 4, 4, 2)\n    out = np.zeros((bw * bh, 4, 4, 4), np.uint8)\n    out[..., 3] = a[:, 0, :, :, 0]\n    out[..., 0] = a[:, 0, :, :, 1]\n    out[..., 1] = a[:, 1, :, :, 0]\n    out[..., 2] = a[:, 1, :, :, 1]\n    return _detile(out.reshape(bw * bh, 4, 4, 4), w, h, 4, 4)\n\ndef decode_texture(payload, info):\n    """-> (h, w, 4) uint8 RGBA of the top mip, or None if fmt unknown.\n    fmt 3/5 = CMPR; 4 = CMPR + I8 alpha plane at +csize; 2 = RGB5A3;\n    1 = IA8; 0 = RGBA8."""\n    w, h, fmt = info["w"], info["h"], info["fmt"]\n    if fmt in (3, 4, 5):\n        img = dec_cmpr(payload, w, h)\n        if fmt == 4:\n            alpha = dec_i8(payload[info["csize"]:], w, h)\n            if alpha.shape == img.shape[:2]:\n                img = img.copy()\n                img[:, :, 3] = alpha\n        return img\n    if fmt == 2:\n        return dec_rgb5a3(payload, w, h)\n    if fmt == 1:\n        return dec_ia8(payload, w, h)\n    if fmt == 0:\n        return dec_rgba8(payload, w, h)\n    return None\n\n# ---------------- materials ----------------\n\n_DRIVE = re.compile(rb"[A-Za-z]:\\\\")\n\ndef parse_material(buf, off331, size331):\n    """0x0331 -> {\'name\', \'textures\': [(hash, sourcepath), ...]}.\n    Material name is a 64-byte field at +4.  Each texture slot is a 64-byte\n    truncated source path followed by a u32 hash = the texture asset\'s 0x138E\n    hash (sizes 0x15C/0x1D0/0x244 = 1/2/3 slots)."""\n    p = off331 + 16\n    seg = buf[p:p + size331]\n    name = seg[4:4 + 64].split(b"\\0")[0].decode("latin1")\n    texes = []\n    for m in _DRIVE.finditer(seg):\n        po = m.start()\n        if po + 68 > len(seg):\n            continue\n        path = seg[po:po + 64].split(b"\\0")[0].decode("latin1")\n        h = struct.unpack_from(">I", seg, po + 64)[0]\n        texes.append((h, path))\n    return {"name": name, "textures": texes}\n\ndef model_materials(buf, kids):\n    """A model asset\'s 0x0324 chunks in order -> [parse_material dicts]."""\n    out = []\n    for cid, off, size, flags, ver in kids:\n        if cid != 0x0324:\n            continue\n        m331 = find_child(buf, children(buf, off, size), 0x0331)\n        out.append(parse_material(buf, m331[1], m331[2]) if m331\n                   else {"name": "?", "textures": []})\n    return out\n\n# ---------------- skeletons ----------------\n\nclass Skeleton:\n    def __init__(self, names, local, invbind, parent, hashes):\n        self.names, self.local, self.invbind = names, local, invbind\n        self.parent, self.hashes = parent, hashes\n\ndef skeleton_index(buf):\n    """0x0005 section > 0x138D (type 2) with FULL skeleton data inline.\n    -> {name: (off0384, size0384)}"""\n    out = {}\n    for cid, off, size, flags, ver in top(buf):\n        if cid != 0x0005:\n            continue\n        for c1, o1, s1, f1, v1 in children(buf, off, size):\n            if c1 != 0x138D:\n                continue\n            sub = children(buf, o1, s1)\n            e138e = find_child(buf, sub, 0x138E)\n            e384 = find_child(buf, sub, 0x0384)\n            if e138e and e384:\n                h, atype, nm = asset_header(buf, e138e[1])\n                out[nm] = (e384[1], e384[2])\n    return out\n\ndef parse_skeleton(buf, off384, size384):\n    """0x0384 > [0x0390, 0x0385, 0x0389, 0x0387, 0x038C, 0x038A, 0x038B].\n    0x0385 v12 payload: u8 boneCount, u8, u16, f32, then boneCount x\n    (u32 boneNameHash, u32 boneIndex) sorted by hash, then boneCount x 148-byte\n    matrix records: 16f local 4x4 (row major) + 16f inverse-bind 4x4 + 20 bytes.\n    0x038B: boneCount x 64-byte name slots."""\n    kk = children(buf, off384, size384)\n    e385 = find_child(buf, kk, 0x0385)\n    e38b = find_child(buf, kk, 0x038B)\n    if not e385 or not e38b:\n        return None\n    n = (e385[2] - 8) // 156\n    if n <= 0 or (e385[2] - 8) % 156:\n        return None\n    names = [buf[e38b[1] + 16 + i * 64: e38b[1] + 16 + i * 64 + 64].split(b"\\0")[0]\n             .decode("latin1") for i in range(min(n, e38b[2] // 64))]\n    while len(names) < n:\n        names.append("bone%d" % len(names))\n    p = e385[1] + 16 + 8\n    hashes = {}\n    for i in range(n):\n        h, idx = struct.unpack_from(">II", buf, p + i * 8)\n        hashes[h] = idx\n    base = p + n * 8\n    local, invbind = [], []\n    for i in range(n):\n        local.append(np.array(struct.unpack_from(">16f", buf, base + i * 148))\n                     .reshape(4, 4).astype(np.float64))\n        invbind.append(np.array(struct.unpack_from(">16f", buf, base + i * 148 + 64))\n                       .reshape(4, 4).astype(np.float64))\n    world = []\n    for ib in invbind:\n        try:\n            world.append(np.linalg.inv(ib))\n        except np.linalg.LinAlgError:\n            world.append(np.eye(4))\n    parent = [-1] * n\n    for i in range(n):\n        try:\n            pw = np.linalg.inv(local[i]) @ world[i]\n        except np.linalg.LinAlgError:\n            continue\n        best, bestd = -1, 1e-3\n        for j in range(n):\n            if j == i:\n                continue\n            d = np.abs(pw - world[j]).sum()\n            if d < bestd:\n                bestd, best = d, j\n        parent[i] = best\n    parent[0] = -1\n    for i in range(n):                      # break accidental cycles\n        seen, j = set(), i\n        while j != -1:\n            if j in seen:\n                parent[i] = -1\n                break\n            seen.add(j)\n            j = parent[j]\n    return Skeleton(names, local, invbind, parent, hashes)\n\n# ---------------- glTF container ----------------\n\nclass Glb:\n    def __init__(self):\n        self.buf = bytearray(); self.views = []; self.accessors = []\n        self.images = []; self.textures = []; self._texcache = {}\n    def add_texture(self, key, png_bytes):\n        if key in self._texcache:\n            return self._texcache[key]\n        vid = self._view(png_bytes)\n        self.images.append({"name": key, "bufferView": vid, "mimeType": "image/png"})\n        self.textures.append({"source": len(self.images) - 1, "sampler": 0})\n        ti = len(self.textures) - 1\n        self._texcache[key] = ti\n        return ti\n    def _view(self, data_bytes, target=None):\n        while len(self.buf) % 4:\n            self.buf.append(0)\n        off = len(self.buf); self.buf += data_bytes\n        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data_bytes)}\n        if target:\n            v["target"] = target\n        self.views.append(v)\n        return len(self.views) - 1\n    def accessor(self, arr, comptype, atype, target=None, mn=None, mx=None):\n        a = np.asarray(arr)\n        vid = self._view(a.tobytes(), target)\n        acc = {"bufferView": vid, "componentType": comptype,\n               "count": int(a.shape[0]), "type": atype}\n        if mn is not None:\n            acc["min"] = [float(x) for x in mn]; acc["max"] = [float(x) for x in mx]\n        self.accessors.append(acc)\n        return len(self.accessors) - 1\n    def write(self, gltf, path):\n        gltf["buffers"] = [{"byteLength": len(self.buf)}]\n        gltf["bufferViews"] = self.views; gltf["accessors"] = self.accessors\n        if self.images:\n            gltf["images"] = self.images; gltf["textures"] = self.textures\n            gltf["samplers"] = [{"wrapS": 10497, "wrapT": 10497,\n                                 "magFilter": 9729, "minFilter": 9987}]\n        js = json.dumps(gltf).encode("utf-8"); js += b" " * ((4 - len(js) % 4) % 4)\n        bn = bytes(self.buf); bn += b"\\0" * ((4 - len(bn) % 4) % 4)\n        total = 12 + 8 + len(js) + 8 + len(bn)\n        with open(path, "wb") as f:\n            f.write(struct.pack("<III", 0x46546C67, 2, total))\n            f.write(struct.pack("<II", len(js), 0x4E4F534A)); f.write(js)\n            f.write(struct.pack("<II", len(bn), 0x004E4942)); f.write(bn)\n\ndef smooth_normals(P, flat_tris):\n    P = np.asarray(P, dtype=np.float64)\n    tri = np.asarray(flat_tris, dtype=np.int64).reshape(-1, 3)\n    fn = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])\n    vn = np.zeros_like(P)\n    for k in range(3):\n        np.add.at(vn, tri[:, k], fn)\n    _, inv = np.unique(np.round(P, 4), axis=0, return_inverse=True)\n    merged = np.zeros((inv.max() + 1, 3)); np.add.at(merged, inv, vn)\n    vn = merged[inv]\n    n = np.linalg.norm(vn, axis=1, keepdims=True); n[n < 1e-12] = 1\n    return (vn / n).astype(np.float32)\n'
_SK_GEO_SOURCE = '#!/usr/bin/env python3\n"""sk_geo.py - Skylanders SuperChargers Racing (Wii) geometry decoding.\n\nModel asset layout (0x0026 type 3):\n    0x138E header, 0x0322 summary, 0x0324 materials xM, 0x033A bone map,\n    0x0325 DESCRIPTOR blocks xL (one per LOD; 0x0326 + 0x0327 descriptors),\n    0x0335 separator, 0x0325 GEOMETRY blocks xL (0x0326 + 0x1389 display list\n    + 0x1388 record array + optional 0x0337 > [0x1770 skin table, 0x1771\n    positions+normals]).  Descriptor block i pairs with geometry block i.\n\n0x0327 v7 descriptor: 14 u32 header + u[8] u32 bone palette at u[14]:\n    u[0]=1  u[1] 0x1388 byte offset  u[2] record stride  u[3] format word\n    u[4] record count  u[5] DL byte offset  u[6] DL byte length  u[7]=0\n    u[8] palette length  u[9]/u[12] 0xffffffff (runtime)  u[10]=4\n    u[11] uninitialized garbage  u[13]>>24 = MATERIAL INDEX\n    palette entries are SKELETON bone indices.\n\nFormat word u[3]: nUV=(u3>>5)&7, colour=u3&2, 0x0800=direct GX matrix index\n(unseen in this game, supported anyway).  The rest of the record layout is\nsolved per descriptor from the stride + data (solve_layout below):\n    AoS:   pos f32x3 [+ nrm s8x4] [+ clr u32] + nUV x uv(2xs16/1024)\n           [+ skin8 | + pad4]\n    split: (pos+nrm live in 0x1771) [clr] + nUV x uv [+ skin8]\n    skin8 = 4 x u8 skeleton bone indices + 4 x u8 weights (sum 255) INLINE.\n\n0x1771: 16-byte records: 3xf32 position + s8[3]/64 normal + pad.\n0x1770: same register-machine skin table as earlier GX Goliath builds\n(kept as fallback; the inline skin8 field supersedes it).\n"""\nimport struct\nimport numpy as np\nimport sklib\n\n# ---------------- format word ----------------\n\ndef nuv(fmt):     return (fmt >> 5) & 7\ndef has_clr(fmt): return bool(fmt & 0x0002)\ndef has_mtx(fmt): return bool(fmt & 0x0800)\ndef has_nrm(fmt): return bool(fmt & 0x1000)   # normal is a DL-indexed attribute\n\n# ---------------- model enumeration ----------------\n\ndef get_models(buf):\n    """-> [(name, hash, modeldict)] for every type-3 0x0026 asset.\n    modeldict: {\'mats\': [...], \'lods\': [ {descs, g1388, g1389, g1770, g1771} ]}"""\n    out = []\n    for name, atype, h, off, size, kids in sklib.assets(buf):\n        if atype != 3:\n            continue\n        m = parse_model(buf, kids)\n        if m["lods"]:\n            out.append((name, h, m))\n    return out\n\ndef parse_model(buf, kids):\n    mats = sklib.model_materials(buf, kids)\n    dblocks, gblocks = [], []\n    for cid, off, size, flags, ver in kids:\n        if cid != 0x0325:\n            continue\n        kk = sklib.children(buf, off, size)\n        if any(c[0] == 0x0327 for c in kk):\n            dblocks.append([parse_descriptor(buf, c[1], c[2])\n                            for c in kk if c[0] == 0x0327])\n        elif any(c[0] in (0x1388, 0x1389) for c in kk):\n            g = {}\n            for c in kk:\n                if c[0] in (0x1388, 0x1389):\n                    g[c[0]] = (c[1] + 16, c[2])\n                elif c[0] == 0x0337:\n                    for c2 in sklib.children(buf, c[1], c[2]):\n                        if c2[0] in (0x1770, 0x1771):\n                            g[c2[0]] = (c2[1] + 16, c2[2])\n            gblocks.append(g)\n    lods = []\n    for i, descs in enumerate(dblocks):\n        if i < len(gblocks) and 0x1388 in gblocks[i] and 0x1389 in gblocks[i]:\n            lods.append({"descs": descs, "geo": gblocks[i]})\n    return {"mats": mats, "lods": lods}\n\ndef parse_descriptor(buf, off, size):\n    n = size // 4\n    u = struct.unpack_from(">%dI" % n, buf, off + 16)\n    npal = u[8]\n    pal = list(u[14:14 + npal]) if 14 + npal <= n else []\n    return {"voff": u[1], "stride": u[2], "fmt": u[3], "nv": u[4],\n            "dloff": u[5], "dllen": u[6], "mat": u[13] >> 24, "pal": pal}\n\n# ---------------- record layout solver ----------------\n\ndef _score_skin(buf, vbase, stride, off, nv):\n    """Fraction of sampled records whose [4 bones][4 weights] field at `off`\n    has weights summing to ~255."""\n    good = tot = 0\n    for i in range(0, nv, max(1, nv // 48)):\n        r = vbase + i * stride + off\n        w = buf[r + 4:r + 8]\n        tot += 1\n        if 250 <= (w[0] + w[1] + w[2] + w[3]) <= 255 and w[0] > 0:\n            good += 1\n    return good / max(1, tot)\n\ndef _score_nrm(buf, vbase, stride, off, nv):\n    """Fraction of sampled records whose s8x3/64 vector at `off` is unit-ish."""\n    good = tot = 0\n    for i in range(0, nv, max(1, nv // 48)):\n        r = vbase + i * stride + off\n        v = struct.unpack_from(">3b", buf, r)\n        L = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) / 4096.0\n        tot += 1\n        if 0.80 <= L <= 1.21:\n            good += 1\n    return good / max(1, tot)\n\ndef solve_layout(buf, geo, d):\n    """-> dict(split, posoff, nrmoff, clroff, uvoff, skinoff) with None for\n    absent fields; offsets are within one 0x1388 record."""\n    fmt, stride, nv = d["fmt"], d["stride"], d["nv"]\n    n = nuv(fmt)\n    clr = 4 if has_clr(fmt) else 0\n    vbase = geo[0x1388][0] + 4 + d["voff"]\n    have1771 = 0x1771 in geo\n\n    def compose(split, nrm, skin, pad):\n        base = (0 if split else 12) + nrm + clr + 4 * n + skin + pad\n        return base == stride\n\n    cands = []\n    for split in ([True, False] if have1771 else [False]):\n        for nrm in ([0] if (split or not has_nrm(fmt)) else [0, 4]):\n            for skin in (8, 0):\n                for pad in (0, 4, 8):\n                    if compose(split, nrm, skin, pad):\n                        cands.append((split, nrm, skin, pad))\n    best, bestscore = None, -1.0\n    for split, nrm, skin, pad in cands:\n        pos = None if split else 0\n        p = 0 if split else 12\n        nrmoff = None\n        if nrm:\n            nrmoff = p; p += 4\n        clroff = None\n        if clr:\n            clroff = p; p += 4\n        uvoff = p if n else None\n        p += 4 * n\n        skinoff = None\n        score = 1.0\n        if skin:\n            skinoff = p\n            score *= _score_skin(buf, vbase, stride, skinoff, nv)\n        if nrm:\n            score *= _score_nrm(buf, vbase, stride, nrmoff, nv)\n        if not split and nv:\n            f = struct.unpack_from(">3f", buf, vbase)\n            if not all(abs(x) < 1e6 or x != x for x in f):\n                score *= 0.1\n        # prefer simpler layouts on ties (fewer speculative fields)\n        score -= 0.001 * (pad // 4)\n        if score > bestscore:\n            bestscore = score\n            best = {"split": split, "posoff": pos, "nrmoff": nrmoff,\n                    "clroff": clroff, "uvoff": uvoff, "skinoff": skinoff,\n                    "score": score}\n    if best is None:\n        raise ValueError("no record layout fits stride %d fmt %#x" % (stride, fmt))\n    return best\n\n# ---------------- display list ----------------\n\n_DL_PRIMS = {0x80, 0x88, 0x90, 0x98, 0xA0, 0xA8, 0xB0}\n\ndef slot_layout(fmt, lay):\n    out = []\n    if has_mtx(fmt):\n        out.append("mtx")\n    out.append("pos")\n    if has_nrm(fmt):\n        out.append("nrm")\n    if has_clr(fmt):\n        out.append("clr")\n    out += ["uv%d" % i for i in range(nuv(fmt))]\n    return out\n\ndef parse_dl(buf, geo, d, lay, n1771):\n    """Descriptor\'s display-list slice -> list of (prim, [index tuples]).\n    Index sizes: pos/nrm by the 0x1771 count for split meshes (else the\n    record count); clr/uv by the total 0x1388 record estimate; candidate\n    fallbacks (all-u16, all-u8, nv-derived) if the walk does not fit."""\n    base, size = geo[0x1389]\n    total = struct.unpack_from(">I", buf, base)[0]\n    start = base + 4 + d["dloff"]\n    end = min(start + d["dllen"], base + 4 + total)\n    slots = slot_layout(d["fmt"], lay)\n    n1388 = geo[0x1388][1] // max(1, d["stride"])\n\n    def sz(kind, arrlen):\n        if kind == "mtx":\n            return 1\n        return 1 if arrlen <= 256 else 2\n\n    poslen = n1771 if lay["split"] else d["nv"]\n    derived = [sz(s, poslen if s in ("pos", "nrm") else\n                  (n1388 if lay["split"] else d["nv"])) for s in slots]\n    derived2 = [sz(s, poslen if s in ("pos", "nrm") else n1388) for s in slots]\n    derived3 = [sz(s, poslen if s in ("pos", "nrm") else d["nv"]) for s in slots]\n    cands = [derived, derived2, derived3,\n             [1 if s == "mtx" else 2 for s in slots], [1] * len(slots)]\n    for sizes in cands:\n        out = _walk_dl(buf, start, end, sizes)\n        if out is not None:\n            return out\n    raise ValueError("display list does not parse (fmt=%#x dloff=%#x)"\n                     % (d["fmt"], d["dloff"]))\n\ndef _walk_dl(buf, start, end, sizes):\n    p, out = start, []\n    vstride = sum(sizes)\n    while p < end:\n        c = buf[p]\n        if c == 0x00:\n            p += 1\n            continue\n        if (c & 0xF8) not in _DL_PRIMS:\n            return None\n        cnt = struct.unpack_from(">H", buf, p + 1)[0]\n        need = cnt * vstride\n        if p + 3 + need > end:\n            return None\n        verts = []\n        q = p + 3\n        for _ in range(cnt):\n            tup = []\n            for s in sizes:\n                tup.append(buf[q] if s == 1 else (buf[q] << 8) | buf[q + 1])\n                q += s\n            verts.append(tuple(tup))\n        out.append((c & 0xF8, verts))\n        p += 3 + need\n    return out\n\ndef dl_triangles(ops):\n    """GX strips: even i -> (v[i+1], v[i], v[i+2]), odd i -> (v[i], v[i+1],\n    v[i+2]); degenerates dropped."""\n    tris = []\n    for prim, verts in ops:\n        if prim == 0x90:                      # GX_TRIANGLES\n            for i in range(0, len(verts) - 2, 3):\n                tris.append((verts[i], verts[i + 1], verts[i + 2]))\n        elif prim == 0x98:                    # GX_TRIANGLESTRIP\n            for i in range(len(verts) - 2):\n                a, b, c = verts[i], verts[i + 1], verts[i + 2]\n                if a == b or b == c or a == c:\n                    continue\n                tris.append((b, a, c) if i % 2 == 0 else (a, b, c))\n        elif prim == 0xA0:                    # GX_TRIANGLEFAN\n            for i in range(1, len(verts) - 1):\n                tris.append((verts[0], verts[i], verts[i + 1]))\n    return tris\n\n# ---------------- attribute arrays ----------------\n\ndef _nrm3(b0, b1, b2):\n    x = b0 - 256 if b0 > 127 else b0\n    y = b1 - 256 if b1 > 127 else b1\n    z = b2 - 256 if b2 > 127 else b2\n    return (x / 64.0, y / 64.0, z / 64.0)\n\ndef read_1771(buf, geo):\n    if 0x1771 not in geo:\n        return [], []\n    off, size = geo[0x1771]\n    P, N = [], []\n    for i in range(size // 16):\n        x, y, z = struct.unpack_from(">3f", buf, off + i * 16)\n        P.append((x, y, z))\n        N.append(_nrm3(buf[off + i * 16 + 12], buf[off + i * 16 + 13],\n                       buf[off + i * 16 + 14]))\n    return P, N\n\ndef parse_1770(buf, geo):\n    """Register-machine skin table over the 0x1771 vertex order (fallback\n    when a descriptor has no inline skin8 field).  Bones are skeleton\n    indices."""\n    if 0x1770 not in geo:\n        return []\n    off, size = geo[0x1770]\n    n = size // 4\n    u = struct.unpack_from(">%dI" % n, buf, off)\n    nv, extra = u[0], u[1]\n\n    def chain(i, total):\n        verts = []\n        while len(verts) < total:\n            ninfl, secverts = u[i], u[i + 1]\n            i += 2\n            if not (1 <= ninfl <= 4) or secverts == 0 or secverts > total:\n                raise ValueError("bad skin section (%d,%d) at word %d"\n                                 % (ninfl, secverts, i - 2))\n            regs = list(u[i:i + ninfl])\n            i += ninfl\n            first, done = True, 0\n            while done < secverts:\n                if not first:\n                    while True:\n                        slot, bone = u[i], u[i + 1]\n                        i += 2\n                        if slot >= ninfl:\n                            raise ValueError("bad skin slot %d at word %d"\n                                             % (slot, i - 2))\n                        regs[slot] = bone\n                        if u[i] == 0:\n                            i += 1\n                            continue\n                        break\n                cnt = u[i]\n                i += 1\n                first = False\n                if cnt == 0 or done + cnt > secverts:\n                    raise ValueError("bad skin count %d at word %d" % (cnt, i - 1))\n                if ninfl > 1:\n                    for k in range(cnt):\n                        w = u[i + k]\n                        wb = ((w >> 24) & 255, (w >> 16) & 255,\n                              (w >> 8) & 255, w & 255)\n                        verts.append((tuple(regs), wb[:ninfl]))\n                    i += cnt\n                else:\n                    verts.extend(((regs[0],), (255,)) for _ in range(cnt))\n                done += cnt\n        return verts, i\n\n    verts, i = chain(2, nv)\n    if extra:\n        more, i = chain(i, extra)\n        verts.extend(more)\n    return verts\n\n# ---------------- submesh assembly ----------------\n\ndef decode_submesh(buf, geo, d, pos1771=None, nrm1771=None, skin1770=None):\n    """-> dict(pos, nrm, uv, joints, weights, tris, mat).  Local vertex\n    arrays; joints are SKELETON bone indices."""\n    lay = solve_layout(buf, geo, d)\n    n1771 = len(pos1771) if pos1771 else 0\n    ops = parse_dl(buf, geo, d, lay, n1771)\n    tris = dl_triangles(ops)\n    slots = slot_layout(d["fmt"], lay)\n    s_mtx = slots.index("mtx") if "mtx" in slots else None\n    s_pos = slots.index("pos")\n    s_uv0 = slots.index("uv0") if "uv0" in slots else None\n    vbase = geo[0x1388][0] + 4 + d["voff"]\n    stride = d["stride"]\n    ntab = len(skin1770) if skin1770 else 0\n\n    remap, P, N, UV, J, W = {}, [], [], [], [], []\n    otris = []\n\n    def rigid_bone(tup):\n        if not d["pal"]:\n            return None\n        i = tup[s_mtx] // 3 if s_mtx is not None else 0\n        return d["pal"][i] if i < len(d["pal"]) else d["pal"][0]\n\n    for t in tris:\n        ot = []\n        for tup in t:\n            vi = remap.get(tup)\n            if vi is None:\n                vi = len(P)\n                remap[tup] = vi\n                pi = tup[s_pos]\n                # record index for UV/clr/skin: local for split, = pi for AoS\n                ri = tup[s_uv0] if (lay["split"] and s_uv0 is not None) else pi\n                r = vbase + ri * stride\n                if lay["split"]:\n                    P.append(pos1771[pi] if pi < n1771 else (0.0, 0.0, 0.0))\n                    N.append(nrm1771[pi] if pi < n1771 else (0.0, 0.0, 1.0))\n                else:\n                    P.append(struct.unpack_from(">3f", buf, r + lay["posoff"]))\n                    if lay["nrmoff"] is not None:\n                        N.append(_nrm3(buf[r + lay["nrmoff"]],\n                                       buf[r + lay["nrmoff"] + 1],\n                                       buf[r + lay["nrmoff"] + 2]))\n                if lay["uvoff"] is not None and s_uv0 is not None:\n                    ur = vbase + tup[s_uv0] * stride + lay["uvoff"]\n                    a, b = struct.unpack_from(">2h", buf, ur)\n                    UV.append((a / 1024.0, b / 1024.0))\n                else:\n                    UV.append((0.0, 0.0))\n                # skin: inline > 1770 table > rigid palette.  Inline skin is\n                # only addressable when the record has a DL index (UV slot for\n                # split meshes; AoS records are indexed by position).\n                if lay["skinoff"] is not None and \\\n                        (not lay["split"] or s_uv0 is not None):\n                    sb = r + lay["skinoff"]\n                    bones = tuple(buf[sb:sb + 4])\n                    ws = tuple(buf[sb + 4:sb + 8])\n                    J.append(bones)\n                    W.append(ws)\n                elif lay["split"] and skin1770 and pi < ntab and s_mtx is None:\n                    J.append(skin1770[pi][0])\n                    W.append(skin1770[pi][1])\n                else:\n                    rb = rigid_bone(tup)\n                    if rb is not None:\n                        J.append((rb,))\n                        W.append((255,))\n            ot.append(vi)\n        otris.append(tuple(ot))\n    return {"pos": P, "nrm": N if N else None, "uv": UV,\n            "joints": J if J else None, "weights": W if W else None,\n            "tris": otris, "mat": d["mat"], "layout": lay}\n'
_GOLIATH_SOURCE = '#!/usr/bin/env python3\n"""\ngoliath_pkz.py - library for reading, editing and WRITING Beenox "Goliath"\nengine .pkz packages and their chunk trees. The write path is the piece the\nextraction-era tools never had: it enables pak injection / modding.\n\nFormat truth (validated against retail data + PKZLib, the engine-class\nrecreation at https://github.com/goliathret/PKZLib):\n\nPKZ container, 32-bit header (Bee Movie / MvA / SD / EOT - X360, PS3, Wii):\n  +0x00 u32 BE magic       BABEB1B0\n  +0x04 u32 BE sectorSize  0x8000  (compressed slot size)\n  +0x08 u32 BE payloadOff  start of slot data (header+table live below)\n  +0x0C u32 BE dcap        LARGEST per-slot decompressed size (engine\'s\n                           decompress buffer allocation!)\n  +0x10 u32 BE nSlots\n  +0x14 u32 BE zsize       (nSlots-1)*sectorSize + used bytes of last slot\n  +0x18 u32 BE dsize       total decompressed size\n  +0x1C u32[nSlots] BE     cumulative DECOMPRESSED sizes (last == dsize)\n  payloadOff: nSlots slots of exactly sectorSize bytes, each an INDEPENDENT\n  zlib stream (retail streams are cut unterminated at the slot boundary; the\n  reader decompresses per-slot until the table says stop, so COMPLETE streams\n  with zero padding are equally valid - that is what we write). File is padded\n  to payloadOff + nSlots*sectorSize.\n\n  Stored variant: no BABEB1B0; the file IS the raw chunk stream (first chunk\n  0x0001 Root spans the file).\n\n64-bit header (TASM2 / SSCR era), per PKZLib BUCompressHeader:\n  +0x00 u32 magic, +0x04 u32 sectorSize, +0x08 u32 totalHeaderSize\n  (= payload offset), +0x0C u32 bigChunkSize (dcap), +0x10 u64 compDataSize,\n  +0x18 u64 uncompFileSize, +0x20 u32 nbSectorsCompData, +0x24 u32 pad,\n  +0x28 u32[ceil(uncomp/sector)] seek lookup (uncomp sector -> slot),\n  then u64[nSlots] cumulative decompressed sizes. Read supported (exact\n  layout, scan fallback); write NOT yet.\n\nEndianness: X360/PS3/Wii packages are big-endian; the 3DS SKU (SSCR 3DS)\nis LITTLE-endian throughout - container fields, seek tables and chunk\nheaders. sniff() reports it and read_pkz()/parse_package() take it from\nthere. The wide-length flag is the top byte of the u32 id in both cases\n(LE byte order puts it 4th on disk - PKZLib\'s idBytes[0] check is BE-only).\n\nChunk header (per PKZLib CMChunk, validated on 842K EOT chunks, 0 errors):\n  u32 BE id (mask 0x00FFFFFF; top byte 0x80 -> u64 length instead of u32)\n  u16 BE version\n  u16 BE hasChildren\n  u32/u64 BE length\nEditing model: parse() the raw package into a Chunk tree, mutate leaf .data /\nchildren lists, serialize() - all ancestor lengths are recomputed, so edits of\nany size are safe.\n\nBUCRC: the engine\'s resource id = CRC32 (std polynomial) of the UPPERCASED\nname, quality suffix ([Hi]/[HI]) stripped. See res_id().\n"""\nimport struct, zlib, os\ntry:\n    from goliath_chunk_names import chunk_name\nexcept ImportError:                     # allow use as a lone file\n    def chunk_name(t): return f\'Unknown_{t:04X}\'\n\nMAGIC = 0xBABEB1B0\nSECTOR = 0x8000\n\n\n# ---------------------------------------------------------------- BUCRC ----\n\ndef bucrc(name: str) -> int:\n    """Goliath BUCRC: CRC32 over the uppercased string."""\n    return zlib.crc32(name.upper().encode(\'ascii\')) & 0xFFFFFFFF\n\n\ndef res_id(name: str) -> int:\n    """Resource id as stored in GenSub_ResourceHeader: BUCRC of the name\n    without the trailing quality suffix ([Hi], [HI], ...)."""\n    base = name.split(\'[\')[0] if name.endswith(\']\') else name\n    return bucrc(base)\n\n\n# ------------------------------------------------------------- container ----\n\nclass PkzInfo:\n    def __init__(self, **kw):\n        self.__dict__.update(kw)\n\n    def __repr__(self):\n        return (\'PkzInfo(variant={variant!r}, sector={sector:#x}, \'\n                \'payload_off={payload_off:#x}, dcap={dcap:#x}, \'\n                \'n_slots={n_slots}, zsize={zsize:#x}, dsize={dsize:#x})\'\n                ).format(**self.__dict__)\n\n\ndef _sniff_variant(data: bytes, e: str):\n    """Container variant for one endianness (\'>\'/\'<\'), or None."""\n    if len(data) >= 0x28 and struct.unpack_from(e + \'I\', data, 0)[0] == MAGIC:\n        # 64-bit variant keeps u64 sizes at +0x10; in the 32-bit layout that\n        # position holds nSlots (+0x10) which is small, so test the wide dsize.\n        zsize64, dsize64 = struct.unpack_from(e + \'2Q\', data, 0x10)\n        n32 = struct.unpack_from(e + \'I\', data, 0x10)[0]\n        z32, d32 = struct.unpack_from(e + \'2I\', data, 0x14)\n        if 0 < n32 < 0x100000 and 28 + 4 * n32 <= len(data) and z32 <= len(data) and d32 >= z32:\n            return \'pkz32\'\n        if dsize64 >= zsize64 > 0:\n            return \'pkz64\'\n        return None\n    # stored: the file IS the chunk stream; root chunk spans it (narrow or wide)\n    if len(data) >= 12:\n        t, ver, hasch = struct.unpack_from(e + \'IHH\', data, 0)\n        if (t & 0xFFFFFF) == 1:\n            if (t >> 24) == 0x80:\n                if len(data) >= 16 and 16 + struct.unpack_from(e + \'Q\', data, 8)[0] == len(data):\n                    return \'stored\'\n            elif 12 + struct.unpack_from(e + \'I\', data, 8)[0] == len(data):\n                return \'stored\'\n    return None\n\n\ndef sniff(data: bytes) -> str:\n    """\'pkz32\', \'pkz64\', \'stored\' (big-endian), the same + \'le\' suffix\n    (little-endian, e.g. SSCR 3DS), or \'unknown\'."""\n    for e, suffix in ((\'>\', \'\'), (\'<\', \'le\')):\n        kind = _sniff_variant(data, e)\n        if kind:\n            return kind + suffix\n    return \'unknown\'\n\n\ndef read_pkz(data: bytes):\n    """Decompress a .pkz (any variant/endianness, or stored).\n    Returns (raw, PkzInfo); info.endian is the struct char for the chunks."""\n    kind = sniff(data)\n    e = \'<\' if kind.endswith(\'le\') else \'>\'\n    if kind.startswith(\'stored\'):\n        return data, PkzInfo(variant=kind, endian=e, sector=0, payload_off=0,\n                             dcap=0, n_slots=0, zsize=len(data), dsize=len(data))\n    if kind.startswith(\'pkz32\'):\n        magic, cs, bo, dcap, nc, tz, ts = struct.unpack(e + \'7I\', data[:28])\n        tab = struct.unpack_from(f\'{e}{nc}I\', data, 28)\n        info = PkzInfo(variant=kind, endian=e, sector=cs, payload_off=bo,\n                       dcap=dcap, n_slots=nc, zsize=tz, dsize=ts)\n        out = bytearray()\n        prev = 0\n        rem = tz\n        for i, cum in enumerate(tab):\n            want = cum - prev\n            prev = cum\n            feed = min(cs, rem)\n            do = zlib.decompressobj()\n            raw = do.decompress(data[bo + i * cs: bo + i * cs + feed], want)\n            if len(raw) != want:\n                raise ValueError(f\'slot {i}: expected {want:#x} bytes, got {len(raw):#x}\')\n            out += raw\n            rem -= cs\n        if len(out) != ts:\n            raise ValueError(f\'dsize mismatch: {len(out):#x} != {ts:#x}\')\n        return bytes(out), info\n    if kind.startswith(\'pkz64\'):\n        magic, cs, bo, dcap = struct.unpack_from(e + \'4I\', data, 0)\n        tz, ts = struct.unpack_from(e + \'2Q\', data, 0x10)\n        nc = struct.unpack_from(e + \'I\', data, 0x20)[0]\n        info = PkzInfo(variant=kind, endian=e, sector=cs, payload_off=bo,\n                       dcap=dcap, n_slots=nc, zsize=tz, dsize=ts)\n        # exact layout (PKZLib BUCompressHeader): u32 seek lookup per\n        # uncompressed sector at +0x28, then the u64 cumulative table.\n        tab = None\n        p = 0x28 + 4 * ((ts + cs - 1) // cs)\n        if p + 8 * nc <= len(data):\n            cand = struct.unpack_from(f\'{e}{nc}Q\', data, p)\n            if cand[-1] == ts and all(cand[i] < cand[i + 1] for i in range(nc - 1)):\n                tab = cand\n        if tab is None:\n            tab = _find_table64(data, nc, ts, e)\n        if tab is None:\n            raise ValueError(\'pkz64: cumulative table not found\')\n        out = bytearray()\n        prev = 0\n        for i, cum in enumerate(tab):\n            want = cum - prev\n            prev = cum\n            do = zlib.decompressobj()\n            raw = do.decompress(data[bo + i * cs: bo + (i + 1) * cs], want)\n            if len(raw) != want:\n                raise ValueError(f\'slot {i}: expected {want:#x}, got {len(raw):#x}\')\n            out += raw\n        return bytes(out), info\n    raise ValueError(\'not a Goliath package (bad magic / layout)\')\n\n\ndef _find_table64(data, nchunks, dsize, e=\'>\'):\n    end = min(len(data), 0x200000)\n    for p in range(0x28, end, 4):\n        if p + 8 * nchunks > len(data):\n            break\n        if struct.unpack_from(e + \'Q\', data, p + 8 * (nchunks - 1))[0] != dsize:\n            continue\n        vals = struct.unpack_from(f\'{e}{nchunks}Q\', data, p)\n        if vals[0] > 0 and all(vals[i] < vals[i + 1] for i in range(nchunks - 1)):\n            return vals\n    return None\n\n\ndef _fit_slot(raw, pos, sector, level, cap):\n    """Largest n so that zlib.compress(raw[pos:pos+n]) fits in `sector` bytes.\n    Returns (n, stream). Complete Z_FINISH streams only."""\n    limit = min(len(raw) - pos, cap)\n    lo = 1\n    hi = min(limit, sector * 8)         # optimistic 8:1 starting window\n    best = None\n    # grow hi while it still fits\n    while True:\n        z = zlib.compress(raw[pos:pos + hi], level)\n        if len(z) <= sector:\n            best = (hi, z)\n            if hi == limit:\n                return best\n            hi = min(limit, hi * 2)\n        else:\n            break\n    lo = best[0] if best else 0\n    # bisect largest fitting n in (lo, hi)\n    while lo + 1 < hi:\n        mid = (lo + hi) // 2\n        z = zlib.compress(raw[pos:pos + mid], level)\n        if len(z) <= sector:\n            best = (mid, z)\n            lo = mid\n        else:\n            hi = mid\n    if best is None:\n        raise ValueError(\'single byte does not fit a slot?!\')\n    return best\n\n\ndef write_pkz(raw: bytes, sector: int = SECTOR, level: int = 9,\n              slot_cap: int = 0x40000) -> bytes:\n    """Compress raw chunk data into a 32-bit-header .pkz the engine reader\n    accepts. Each slot holds one complete zlib stream (<= sector bytes,\n    zero-padded); dcap is set to the real max per-slot decompressed size.\n    slot_cap bounds per-slot decompressed size (engine allocates dcap)."""\n    slots = []\n    cums = []\n    pos = 0\n    while pos < len(raw):\n        n, z = _fit_slot(raw, pos, sector, level, slot_cap)\n        pos += n\n        slots.append(z)\n        cums.append(pos)\n    nc = len(slots)\n    dcap = max((cums[i] - (cums[i - 1] if i else 0)) for i in range(nc)) if nc else 0\n    zsize = (nc - 1) * sector + len(slots[-1]) if nc else 0\n    header_end = 28 + 4 * nc\n    bo = (header_end + sector - 1) // sector * sector     # slot-aligned like retail\n    out = bytearray()\n    out += struct.pack(\'>7I\', MAGIC, sector, bo, dcap, nc, zsize, len(raw))\n    out += struct.pack(f\'>{nc}I\', *cums)\n    out += b\'\\0\' * (bo - len(out))\n    for z in slots:\n        out += z + b\'\\0\' * (sector - len(z))\n    return bytes(out)\n\n\n# ------------------------------------------------------------ chunk tree ----\n\nclass Chunk:\n    """One chunk. Containers hold .children (+ .tail slack bytes, preserved\n    verbatim); leaves hold .data. Lengths are recomputed on serialize."""\n    __slots__ = (\'type\', \'flags\', \'version\', \'has_children\', \'wide\',\n                 \'data\', \'children\', \'tail\', \'src_off\', \'endian\')\n\n    def __init__(self, type_, version=0, has_children=0, data=b\'\',\n                 flags=0, wide=False, endian=\'>\'):\n        self.type = type_               # masked 24-bit id\n        self.flags = flags              # top byte of the raw id (0x80 = wide)\n        self.version = version\n        self.has_children = has_children\n        self.wide = wide                # u64 length field\n        self.endian = endian            # struct char: \'>\' consoles, \'<\' 3DS\n        self.data = data\n        self.children = []\n        self.tail = b\'\'\n        self.src_off = None             # header offset in the parsed package\n\n    @property\n    def name(self):\n        return chunk_name(self.type)\n\n    def find(self, type_):\n        return [c for c in self.children if c.type == type_]\n\n    def find_one(self, type_):\n        got = self.find(type_)\n        if len(got) != 1:\n            raise KeyError(f\'{self.name}: expected 1 child {chunk_name(type_)}, found {len(got)}\')\n        return got[0]\n\n    # -- (de)serialization --\n\n    @staticmethod\n    def parse_stream(data, o, e, endian=\'>\'):\n        """Parse the chunk sequence in data[o:e] -> (chunks, tail_bytes)."""\n        chunks = []\n        p = o\n        while p + 12 <= e:\n            rawid, ver, hasch = struct.unpack_from(endian + \'IHH\', data, p)\n            flags = rawid >> 24\n            wide = flags == 0x80\n            if wide:\n                if p + 16 > e:\n                    break\n                length = struct.unpack_from(endian + \'Q\', data, p + 8)[0]\n                hs = 16\n            else:\n                length = struct.unpack_from(endian + \'I\', data, p + 8)[0]\n                hs = 12\n            t = rawid & 0xFFFFFF\n            if t == 0 or p + hs + length > e:\n                break\n            c = Chunk(t, ver, hasch, flags=flags, wide=wide, endian=endian)\n            c.src_off = p\n            if hasch:\n                c.children, c.tail = Chunk.parse_stream(data, p + hs, p + hs + length, endian)\n            else:\n                c.data = bytes(data[p + hs:p + hs + length])\n            chunks.append(c)\n            p += hs + length\n        return chunks, bytes(data[p:e])\n\n    def payload_length(self):\n        if self.has_children:\n            return (sum(c.total_length() for c in self.children) + len(self.tail))\n        return len(self.data)\n\n    def total_length(self):\n        return (16 if self.wide else 12) + self.payload_length()\n\n    def serialize(self, out: bytearray, positions=None):\n        if positions is not None:\n            positions[id(self)] = len(out)\n        rawid = (self.flags << 24) | self.type\n        length = self.payload_length()\n        if self.wide:\n            out += struct.pack(self.endian + \'IHHQ\', rawid, self.version,\n                               self.has_children, length)\n        else:\n            if length > 0xFFFFFFFF:\n                raise ValueError(f\'{self.name}: payload too large for u32 length\')\n            out += struct.pack(self.endian + \'IHHI\', rawid, self.version,\n                               self.has_children, length)\n        if self.has_children:\n            for c in self.children:\n                c.serialize(out, positions)\n            out += self.tail\n        else:\n            out += self.data\n\n    def pretty(self, depth=0, max_depth=99, lines=None, max_lines=4000):\n        lines = [] if lines is None else lines\n        if len(lines) >= max_lines:\n            return lines\n        what = (f\'{len(self.children)} children\' if self.has_children\n                else f\'{len(self.data)} bytes\')\n        lines.append(f\'{"  " * depth}{self.name} (0x{self.type:04X} v{self.version}) {what}\')\n        if self.has_children and depth < max_depth:\n            for c in self.children:\n                c.pretty(depth + 1, max_depth, lines, max_lines)\n            if self.tail:\n                lines.append(f\'{"  " * (depth + 1)}<{len(self.tail)} tail bytes>\')\n        return lines\n\n\ndef parse_package(raw: bytes, endian=\'>\'):\n    """Raw decompressed package -> (list of root chunks, tail bytes)."""\n    return Chunk.parse_stream(raw, 0, len(raw), endian)\n\n\ndef serialize_package(roots, tail=b\'\', positions=None) -> bytes:\n    """positions: optional dict filled with {id(chunk): header offset}."""\n    out = bytearray()\n    for c in roots:\n        c.serialize(out, positions)\n    out += tail\n    return bytes(out)\n\n\ndef resolve_path(roots, path: str):\n    """Address a chunk: \'TYPE[#index]/TYPE[#index]/...\'; TYPE is hex (0331,\n    0x0331) or an engine name (Geo_MaterialInfo). Returns the Chunk."""\n    from goliath_chunk_names import CHUNK_NAMES\n    byname = {v.lower(): k for k, v in CHUNK_NAMES.items()}\n    nodes = list(roots)\n    node = None\n    for seg in path.strip(\'/\').split(\'/\'):\n        idx = 0\n        if \'#\' in seg:\n            seg, i = seg.split(\'#\')\n            idx = int(i)\n        s = seg.lower()\n        t = byname.get(s)\n        if t is None:\n            t = int(s, 16)\n        matches = [c for c in nodes if c.type == t]\n        if idx >= len(matches):\n            raise KeyError(f\'path segment {seg}#{idx}: only {len(matches)} match(es)\')\n        node = matches[idx]\n        nodes = node.children\n    if node is None:\n        raise KeyError(\'empty path\')\n    return node\n'

def _install_embedded_module(name, source):
    mod = _types.ModuleType(name)
    mod.__file__ = '<embedded:%s>' % name
    _sys.modules[name] = mod
    exec(compile(source, mod.__file__, 'exec'), mod.__dict__)
    return mod

_install_embedded_module('sklib', _SKLIB_SOURCE)
_install_embedded_module('sk_geo', _SK_GEO_SOURCE)
_install_embedded_module('goliath_pkz', _GOLIATH_SOURCE)

# Minimal replacement for sk_import_gltf_accessory used by this importer.
_gltf = _types.ModuleType('sk_import_gltf_accessory')
_gltf.__file__ = '<embedded:sk_import_gltf_accessory>'

def _load_glb(path):
    data = open(path, 'rb').read()
    if len(data) < 20 or data[:4] != b'glTF':
        raise ValueError('Not a binary GLB file')
    magic, version, total = _struct.unpack_from('<4sII', data, 0)
    if version != 2:
        raise ValueError('Only glTF 2.0 GLB is supported')
    p = 12; g = None; b = b''
    while p + 8 <= min(total, len(data)):
        n, typ = _struct.unpack_from('<II', data, p); p += 8
        chunk = data[p:p+n]; p += n
        if typ == 0x4E4F534A:  # JSON
            g = _json.loads(chunk.rstrip(b'\\x00 \\t\\r\\n').decode('utf-8'))
        elif typ == 0x004E4942:  # BIN
            b = chunk
    if g is None:
        raise ValueError('GLB has no JSON chunk')
    return g, b

_COMP = {5120:('b',1),5121:('B',1),5122:('h',2),5123:('H',2),5125:('I',4),5126:('f',4)}
_NCOMP = {'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT2':4,'MAT3':9,'MAT4':16}

def _accessor(g, b, idx):
    import numpy as _np
    a=g['accessors'][idx]; n=int(a.get('count',0)); nc=_NCOMP[a['type']]
    ct=a['componentType']; fmt,sz=_COMP[ct]
    bv=g['bufferViews'][a['bufferView']]
    off=int(bv.get('byteOffset',0))+int(a.get('byteOffset',0))
    stride=int(bv.get('byteStride', nc*sz))
    dtype={5120:_np.int8,5121:_np.uint8,5122:_np.int16,5123:_np.uint16,5125:_np.uint32,5126:_np.float32}[ct]
    if stride == nc*sz:
        arr=_np.frombuffer(b, dtype=dtype, count=n*nc, offset=off).reshape(n,nc).copy()
    else:
        arr=_np.empty((n,nc), dtype=dtype)
        for i in range(n):
            arr[i]=_np.frombuffer(b,dtype=dtype,count=nc,offset=off+i*stride)
    if a['type']=='SCALAR': arr=arr[:,0]
    if a.get('normalized') and ct != 5126:
        arr=arr.astype(_np.float64)
        if ct in (5120,5122):
            mx={5120:127.0,5122:32767.0}[ct]; arr=_np.maximum(arr/mx,-1.0)
        else:
            mx={5121:255.0,5123:65535.0,5125:4294967295.0}[ct]; arr=arr/mx
    return arr

def _quat_mat(q):
    import numpy as _np
    x,y,z,w=map(float,q); n=x*x+y*y+z*z+w*w
    if n < 1e-20: return _np.eye(4)
    s=2.0/n; xx=x*x*s; yy=y*y*s; zz=z*z*s; xy=x*y*s; xz=x*z*s; yz=y*z*s; wx=w*x*s; wy=w*y*s; wz=w*z*s
    M=_np.eye(4)
    M[:3,:3]=[[1-(yy+zz),xy-wz,xz+wy],[xy+wz,1-(xx+zz),yz-wx],[xz-wy,yz+wx,1-(xx+yy)]]
    return M

def _node_local(node):
    import numpy as _np
    if 'matrix' in node:
        return _np.array(node['matrix'],dtype=_np.float64).reshape(4,4).T
    T=_np.eye(4); T[:3,3]=node.get('translation',[0,0,0])
    R=_quat_mat(node.get('rotation',[0,0,0,1]))
    S=_np.eye(4); s=node.get('scale',[1,1,1]); S[0,0],S[1,1],S[2,2]=s
    return T@R@S

def _mesh_world(g, mesh_index):
    import numpy as _np
    nodes=g.get('nodes',[]); parents={}
    for i,n in enumerate(nodes):
        for c in n.get('children',[]): parents[c]=i
    candidates=[i for i,n in enumerate(nodes) if n.get('mesh')==mesh_index]
    if not candidates: return _np.eye(4)
    i=candidates[0]; chain=[]
    while True:
        chain.append(i)
        if i not in parents: break
        i=parents[i]
    M=_np.eye(4)
    for j in reversed(chain): M=M@_node_local(nodes[j])
    return M

def _transform_primitive_to_mesh_space(g,b,mi,pi,target_mesh_index=0):
    # Match the ORIGINAL sk_import_gltf_accessory.py behavior:
    # convert each mesh into mesh 0's LOCAL space, instead of baking the GLB
    # root/node transform into the vertices. Blender commonly writes a root
    # axis-conversion rotation; baking it was what turned vehicles on their side.
    import numpy as _np
    prim=g['meshes'][mi]['primitives'][pi]; attrs=prim['attributes']
    if 'POSITION' not in attrs: raise ValueError('Primitive has no POSITION')
    P=_accessor(g,b,attrs['POSITION']).astype(_np.float64)

    Msrc=_mesh_world(g,mi)
    Mdst=_mesh_world(g,target_mesh_index)
    try:
        X=_np.linalg.inv(Mdst) @ Msrc
    except _np.linalg.LinAlgError:
        X=Msrc

    P4=_np.concatenate([P,_np.ones((len(P),1))],axis=1)
    P=(P4@X.T)[:,:3]

    if 'NORMAL' in attrs:
        N=_accessor(g,b,attrs['NORMAL']).astype(_np.float64)
        try: NM=_np.linalg.inv(X[:3,:3]).T
        except _np.linalg.LinAlgError: NM=X[:3,:3]
        N=N@NM.T; L=_np.linalg.norm(N,axis=1); L[L<1e-20]=1; N=N/L[:,None]
    else:
        N=_np.zeros_like(P); N[:,2]=1.0

    if 'indices' in prim:
        I=_accessor(g,b,prim['indices']).astype(_np.int64).reshape(-1)
    else:
        I=_np.arange(len(P),dtype=_np.int64)

    mode=int(prim.get('mode',4)); tris=[]
    if mode==4:
        tris=I[:len(I)//3*3].reshape(-1,3)
    elif mode==5:
        for i in range(len(I)-2):
            a,bx,c=I[i:i+3]
            tris.append((a,bx,c) if i%2==0 else (bx,a,c))
        tris=_np.asarray(tris,dtype=_np.int64).reshape(-1,3)
    elif mode==6:
        tris=_np.asarray([(I[0],I[i],I[i+1]) for i in range(1,len(I)-1)],dtype=_np.int64).reshape(-1,3)
    else:
        raise ValueError('Unsupported glTF primitive mode %d (triangles required)'%mode)

    tris=_np.asarray(tris,dtype=_np.int64).reshape(-1,3)
    return {'P':P,'N':N,'tris':tris,'I':tris.reshape(-1)}

_gltf.load_glb=_load_glb
_gltf.accessor=_accessor
_gltf.transform_primitive_to_mesh_space=_transform_primitive_to_mesh_space
_sys.modules['sk_import_gltf_accessory']=_gltf

import argparse
import io
import math
import struct
from pathlib import Path
from collections import Counter

import numpy as np
from PIL import Image

import sklib
import sk_geo
import goliath_pkz as gp
import sk_import_gltf_accessory as gltf


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def align32(buf: bytearray) -> None:
    while len(buf) % 0x20:
        buf.append(0)


def model_assets(buf: bytes):
    return [a for a in sklib.assets(buf) if a[1] == 3]


def choose_model(buf: bytes, requested: str | None):
    models = model_assets(buf)
    if requested:
        for a in models:
            if a[0].lower() == requested.lower():
                return a
        raise ValueError(
            f'Model "{requested}" not found. Available: '
            + ", ".join(a[0] for a in models)
        )

    # Prefer a static *_SC model with a recognized layout.
    candidates = []
    for a in models:
        try:
            m = sk_geo.parse_model(buf, a[5])
            if not m["lods"]:
                continue
            L = m["lods"][0]
            if not L["descs"]:
                continue
            d = L["descs"][0]
            lay = sk_geo.solve_layout(buf, L["geo"], d)
            slots = sk_geo.slot_layout(d["fmt"], lay)
            score = 0
            if a[0].lower().endswith("_sc"):
                score += 100
            if d["stride"] == 16:
                score += 20
            if slots == ["pos", "nrm", "clr", "uv0"]:
                score += 20
            candidates.append((score, a))
        except Exception:
            pass

    if not candidates:
        raise ValueError("Could not auto-detect a compatible model asset")

    candidates.sort(key=lambda x: (x[0], x[1][4]), reverse=True)
    return candidates[0][1]


def validate_target_layout(buf: bytes, model):
    """Validate and return the target vertex layout kind.

    Supported vehicle layouts:
      - "static16": Mod_SC style, stride 16, color + UV + inline rigid skin
      - "skin12": FireCar / FireCar_Dark style, stride 12, UV + inline skin
    All LODs must use the same family.
    """
    kinds = []
    for li, L in enumerate(model["lods"]):
        if not L["descs"]:
            raise ValueError(f"LOD {li} has no descriptors")
        if len(L["descs"]) != 1:
            raise ValueError(
                f"LOD {li} has {len(L['descs'])} descriptors; "
                "current vehicle importer expects one"
            )

        d = L["descs"][0]
        lay = sk_geo.solve_layout(buf, L["geo"], d)
        slots = sk_geo.slot_layout(d["fmt"], lay)

        if (
            lay["split"]
            and d["stride"] == 16
            and lay["clroff"] == 0
            and lay["uvoff"] == 4
            and lay["skinoff"] == 8
            and slots == ["pos", "nrm", "clr", "uv0"]
        ):
            kinds.append("static16")
        elif (
            lay["split"]
            and d["stride"] == 12
            and lay["clroff"] is None
            and lay["uvoff"] == 0
            and lay["skinoff"] == 4
            and slots == ["pos", "nrm", "uv0"]
        ):
            kinds.append("skin12")
        else:
            raise ValueError(
                f"Unsupported target layout in LOD {li}:\n"
                f"  descriptor={d}\n"
                f"  layout={lay}\n"
                f"  slots={slots}"
            )

    if len(set(kinds)) != 1:
        raise ValueError(f"Mixed target layout families across LODs: {kinds}")
    return kinds[0]


def detect_target_vat_opcode(buf: bytes, model) -> int:
    """
    Return first non-zero GX primitive opcode from LOD0's 0x1389.
    Tested vehicle fixes use 0x9B (GX_TRIANGLESTRIP, VAT3).
    """
    L = model["lods"][0]
    off, size = L["geo"][0x1389]
    raw = buf[off + 4: off + size]
    for x in raw:
        if x:
            return int(x)
    raise ValueError("Could not detect target GX opcode")


def dominant_rigid_joint(buf: bytes, model) -> int:
    """
    Choose the most frequent first joint in target LOD0 skin records.
    Falls back to joint 3.
    """
    try:
        skin = sk_geo.parse_1770(buf, model["lods"][0]["geo"])
        if not skin:
            return 3

        joints = []
        for rec in skin:
            # Normal parser form: ((joint,...), (weight,...))
            if (
                isinstance(rec, tuple)
                and len(rec) >= 1
                and isinstance(rec[0], tuple)
                and rec[0]
            ):
                joints.append(int(rec[0][0]))
        if joints:
            return Counter(joints).most_common(1)[0][0]
    except Exception:
        pass
    return 3


def glb_bounds(g, b):
    pts = []
    for mi, mesh in enumerate(g.get("meshes", [])):
        for pi in range(len(mesh.get("primitives", []))):
            q = gltf.transform_primitive_to_mesh_space(g, b, mi, pi, 0)
            pts.append(q["P"])
    if not pts:
        raise ValueError("GLB contains no geometry")
    P = np.concatenate(pts, axis=0)
    return P.min(0), P.max(0)


def choose_uv_attribute(g, b, primitive):
    """
    Pick the most informative TEXCOORD_n automatically.

    This matters for models such as KART3 where:
      body -> TEXCOORD_0
      tire -> TEXCOORD_1
      rim  -> TEXCOORD_2

    Score UV sets by spread and number of unique values; constant UV sets lose.
    """
    attrs = primitive["attributes"]
    candidates = []
    for key in sorted(k for k in attrs if k.startswith("TEXCOORD_")):
        uv = gltf.accessor(g, b, attrs[key]).astype(np.float64)
        if len(uv) == 0:
            continue
        spread = np.ptp(uv, axis=0)
        unique = len(np.unique(np.round(uv, 5), axis=0))
        score = float(spread[0] + spread[1]) + min(unique, 1000) / 1000.0
        candidates.append((score, key, uv))

    if not candidates:
        return "TEXCOORD_0", np.zeros((0, 2), dtype=np.float64)

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, key, uv = candidates[0]
    return key, uv


def image_from_material(g, b, material_index: int):
    mats = g.get("materials", [])
    mat = mats[material_index] if 0 <= material_index < len(mats) else {}
    pbr = mat.get("pbrMetallicRoughness", {})
    tex = pbr.get("baseColorTexture")

    if tex is not None:
        ti = tex["index"]
        texture = g["textures"][ti]
        source = texture["source"]
        im = g["images"][source]
        bv = g["bufferViews"][im["bufferView"]]
        raw = b[
            bv.get("byteOffset", 0):
            bv.get("byteOffset", 0) + bv["byteLength"]
        ]
        return Image.open(io.BytesIO(raw)).convert("RGBA")

    rgba = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
    color = tuple(
        int(max(0.0, min(1.0, float(v))) * 255.0)
        for v in rgba
    )
    return Image.new("RGBA", (32, 32), color)


# ---------------------------------------------------------------------------
# Atlas
# ---------------------------------------------------------------------------

def _build_square_atlas_legacy(g, b, material_ids, size=256):
    """
    Aspect-ratio-preserving atlas packer for custom GLB materials.

    The old importer forced every material into an equal square cell.  A
    512x256 body texture therefore became ~85x85 when five materials were
    present, visibly stretching and blurring the car.  This packer instead:
      * keeps each source image's aspect ratio;
      * finds the largest common scale that fits all textures in the atlas;
      * packs rectangles with a simple best-area-fit free-rectangle algorithm;
      * extrudes edge pixels into a small gutter to reduce CMPR/mipmap bleeding.

    Returns (atlas_image, material_rects), where each rect is
    (u0, v0, uscale, vscale) for the *actual image area* (not the gutter).
    """
    material_ids = sorted(set(int(x) for x in material_ids))
    if not material_ids:
        material_ids = [0]

    images = {mid: image_from_material(g, b, mid).convert("RGBA")
              for mid in material_ids}

    gutter = 2

    def pack_rects(scale):
        # Build scaled rectangles including a gutter on all sides.
        items = []
        for mid, im in images.items():
            w = max(1, int(round(im.width * scale)))
            h = max(1, int(round(im.height * scale)))
            # CMPR works in 4x4 blocks, but exact multiples are not mandatory
            # here because the final 256x256 texture is encoded as a whole.
            ow, oh = w + 2 * gutter, h + 2 * gutter
            if ow > size or oh > size:
                return None
            items.append((mid, w, h, ow, oh))

        # Large rectangles first makes this tiny MaxRects-like packer robust.
        items.sort(key=lambda x: (x[3] * x[4], max(x[3], x[4])), reverse=True)
        free = [(0, 0, size, size)]
        placed = {}

        for mid, w, h, ow, oh in items:
            best = None
            for i, (fx, fy, fw, fh) in enumerate(free):
                if ow <= fw and oh <= fh:
                    waste = fw * fh - ow * oh
                    short = min(fw - ow, fh - oh)
                    score = (waste, short, fy, fx)
                    if best is None or score < best[0]:
                        best = (score, i, fx, fy, fw, fh)
            if best is None:
                return None

            _, i, fx, fy, fw, fh = best
            free.pop(i)
            placed[mid] = (fx + gutter, fy + gutter, w, h)

            # Guillotine split: choose orientation leaving larger useful areas.
            rw = fw - ow
            bh = fh - oh
            if rw > bh:
                # right spans full original height; bottom only placed width
                if rw > 0:
                    free.append((fx + ow, fy, rw, fh))
                if bh > 0:
                    free.append((fx, fy + oh, ow, bh))
            else:
                # bottom spans full original width; right only placed height
                if bh > 0:
                    free.append((fx, fy + oh, fw, bh))
                if rw > 0:
                    free.append((fx + ow, fy, rw, oh))

            # Remove free rectangles fully contained in another one.
            clean = []
            for a, r in enumerate(free):
                rx, ry, rw2, rh2 = r
                contained = False
                for b, q in enumerate(free):
                    if a == b:
                        continue
                    qx, qy, qw, qh = q
                    if (rx >= qx and ry >= qy and
                        rx + rw2 <= qx + qw and ry + rh2 <= qy + qh):
                        contained = True
                        break
                if not contained and rw2 > 0 and rh2 > 0:
                    clean.append(r)
            free = clean

        return placed

    # Binary-search the largest uniform scale that packs all source textures.
    # Upper bound also prevents individual textures from exceeding the atlas.
    hi = min(
        1.0,
        min((size - 2 * gutter) / max(1, im.width) for im in images.values()),
        min((size - 2 * gutter) / max(1, im.height) for im in images.values()),
    )
    lo = 0.01
    best = None
    best_scale = lo
    for _ in range(28):
        mid = (lo + hi) * 0.5
        attempt = pack_rects(mid)
        if attempt is not None:
            best = attempt
            best_scale = mid
            lo = mid
        else:
            hi = mid

    if best is None:
        raise ValueError("Could not pack GLB material textures into atlas")

    atlas = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    rects = {}

    for mid in material_ids:
        x, y, w, h = best[mid]
        im = images[mid]
        tile = im.resize((w, h), Image.Resampling.LANCZOS)
        atlas.paste(tile, (x, y))

        # Extrude borders into the 2px gutter. This avoids neighbouring atlas
        # colors bleeding into the material when the Wii samples mip levels.
        left = tile.crop((0, 0, 1, h)).resize((gutter, h))
        right = tile.crop((w - 1, 0, w, h)).resize((gutter, h))
        top = tile.crop((0, 0, w, 1)).resize((w, gutter))
        bottom = tile.crop((0, h - 1, w, h)).resize((w, gutter))
        atlas.paste(left, (x - gutter, y))
        atlas.paste(right, (x + w, y))
        atlas.paste(top, (x, y - gutter))
        atlas.paste(bottom, (x, y + h))

        # Fill gutter corners from source corner pixels.
        for cx, cy, sx, sy in (
            (x - gutter, y - gutter, 0, 0),
            (x + w, y - gutter, w - 1, 0),
            (x - gutter, y + h, 0, h - 1),
            (x + w, y + h, w - 1, h - 1),
        ):
            px = tile.getpixel((sx, sy))
            atlas.paste(Image.new("RGBA", (gutter, gutter), px), (cx, cy))

        rects[mid] = (x / size, y / size, w / size, h / size)
        print(
            f"  atlas mat={mid}: {im.width}x{im.height} -> {w}x{h} "
            f"at ({x},{y})"
        )

    print(f"Texture atlas: aspect-preserving packed scale={best_scale:.4f}")
    return atlas, rects


def build_repeat_safe_atlas(g, b, parts, size=512):
    """Build an atlas that preserves glTF REPEAT UVs exactly.

    The previous importer did ``uv = mod(uv, 1)`` before mapping each material
    into a sub-rectangle.  That is fine for isolated vertices, but it breaks a
    triangle that crosses an integer UV boundary: e.g. 0.98 -> 1.02 becomes
    0.98 -> 0.02 and GX interpolates through almost the whole atlas tile.  The
    symptom is small striped / rainbow triangles over an otherwise correct
    texture.

    For each material we instead bake enough copies of its source texture to
    cover the integer UV domain actually used by the GLB.  Raw UVs are then
    mapped linearly into that repeated image, so interpolation across 0/1 (and
    negative / >1 UVs) remains continuous.

    Returns (atlas, rects, domains).  domains[mid] = (umin, vmin, uspan, vspan).
    """
    mids = sorted({int(p['mat']) for p in parts}) or [0]

    domains = {}
    images = {}
    MAX_REPEAT = 32

    for mid in mids:
        uvsets = [np.asarray(p['UV'], dtype=np.float64)
                  for p in parts if int(p['mat']) == mid and len(p['UV'])]
        if uvsets:
            uv = np.concatenate(uvsets, axis=0)
            lo = np.floor(np.min(uv, axis=0)).astype(int)
            hi = np.ceil(np.max(uv, axis=0)).astype(int)
            # A domain whose max is exactly an integer still needs one tile.
            span = np.maximum(1, hi - lo)
        else:
            lo = np.array([0, 0], dtype=int)
            span = np.array([1, 1], dtype=int)

        ru, rv = int(span[0]), int(span[1])
        if ru > MAX_REPEAT or rv > MAX_REPEAT:
            raise ValueError(
                f'Material {mid} uses an extreme repeating UV domain '
                f'{ru}x{rv} tiles. Current safe limit is {MAX_REPEAT}x{MAX_REPEAT}.'
            )

        base = image_from_material(g, b, mid).convert('RGBA')
        tiled = Image.new('RGBA', (base.width * ru, base.height * rv))
        for yy in range(rv):
            for xx in range(ru):
                tiled.paste(base, (xx * base.width, yy * base.height))

        images[mid] = tiled
        domains[mid] = (float(lo[0]), float(lo[1]), float(ru), float(rv))
        if ru != 1 or rv != 1 or lo[0] != 0 or lo[1] != 0:
            print(
                f'  repeat mat={mid}: UV domain '
                f'U[{lo[0]},{lo[0]+ru}] V[{lo[1]},{lo[1]+rv}] -> {ru}x{rv} tiles'
            )

    gutter = 2

    def pack_rects(scale):
        items = []
        for mid, im in images.items():
            w = max(1, int(round(im.width * scale)))
            h = max(1, int(round(im.height * scale)))
            ow, oh = w + 2 * gutter, h + 2 * gutter
            if ow > size or oh > size:
                return None
            items.append((mid, w, h, ow, oh))
        items.sort(key=lambda x: (x[3] * x[4], max(x[3], x[4])), reverse=True)
        free = [(0, 0, size, size)]
        placed = {}
        for mid, w, h, ow, oh in items:
            best = None
            for i, (fx, fy, fw, fh) in enumerate(free):
                if ow <= fw and oh <= fh:
                    score = (fw * fh - ow * oh, min(fw - ow, fh - oh), fy, fx)
                    if best is None or score < best[0]:
                        best = (score, i, fx, fy, fw, fh)
            if best is None:
                return None
            _, i, fx, fy, fw, fh = best
            free.pop(i)
            placed[mid] = (fx + gutter, fy + gutter, w, h)
            rw, bh = fw - ow, fh - oh
            if rw > bh:
                if rw > 0: free.append((fx + ow, fy, rw, fh))
                if bh > 0: free.append((fx, fy + oh, ow, bh))
            else:
                if bh > 0: free.append((fx, fy + oh, fw, bh))
                if rw > 0: free.append((fx + ow, fy, rw, oh))
            clean = []
            for a, r in enumerate(free):
                rx, ry, rw2, rh2 = r
                contained = False
                for bb, q in enumerate(free):
                    if a == bb: continue
                    qx, qy, qw, qh = q
                    if rx >= qx and ry >= qy and rx+rw2 <= qx+qw and ry+rh2 <= qy+qh:
                        contained = True
                        break
                if not contained and rw2 > 0 and rh2 > 0:
                    clean.append(r)
            free = clean
        return placed

    hi = min(
        1.0,
        min((size - 2*gutter) / max(1, im.width) for im in images.values()),
        min((size - 2*gutter) / max(1, im.height) for im in images.values()),
    )
    lo_s, best, best_scale = 0.001, None, 0.001
    for _ in range(30):
        mid_s = (lo_s + hi) * 0.5
        att = pack_rects(mid_s)
        if att is not None:
            best, best_scale, lo_s = att, mid_s, mid_s
        else:
            hi = mid_s
    if best is None:
        raise ValueError('Could not pack repeat-safe material textures into atlas')

    atlas = Image.new('RGBA', (size, size), (0,0,0,255))
    rects = {}
    for mid in mids:
        x, y, w, h = best[mid]
        im = images[mid]
        tile = im.resize((w,h), Image.Resampling.LANCZOS)
        atlas.paste(tile, (x,y))

        # Ordinary edge extrusion is now safe because integer repeat seams are
        # already internal to the baked tiled image rather than atlas borders.
        left = tile.crop((0,0,1,h)).resize((gutter,h))
        right = tile.crop((w-1,0,w,h)).resize((gutter,h))
        top = tile.crop((0,0,w,1)).resize((w,gutter))
        bottom = tile.crop((0,h-1,w,h)).resize((w,gutter))
        atlas.paste(left,(x-gutter,y)); atlas.paste(right,(x+w,y))
        atlas.paste(top,(x,y-gutter)); atlas.paste(bottom,(x,y+h))
        rects[mid] = (x/size, y/size, w/size, h/size)
        print(f'  atlas mat={mid}: repeated {im.width}x{im.height} -> {w}x{h} at ({x},{y})')

    print(f'Texture atlas: REPEAT-safe packed scale={best_scale:.4f}')
    return atlas, rects, domains


# ---------------------------------------------------------------------------
# Wii CMPR encoder
# ---------------------------------------------------------------------------

def rgb565(rgb):
    r, g, b = map(int, rgb)
    return (
        ((r * 31 + 127) // 255) << 11
        | ((g * 63 + 127) // 255) << 5
        | ((b * 31 + 127) // 255)
    )


def unpack565(c):
    return np.array(
        [
            ((c >> 11) & 31) * 255 // 31,
            ((c >> 5) & 63) * 255 // 63,
            (c & 31) * 255 // 31,
        ],
        dtype=np.int16,
    )


def encode_dxt1_block(block):
    px = block[:, :, :3].reshape(-1, 3).astype(np.int16)
    lum = px[:, 0] * 3 + px[:, 1] * 6 + px[:, 2]

    a = rgb565(px[int(np.argmax(lum))])
    z = rgb565(px[int(np.argmin(lum))])

    if a == z:
        if a < 65535:
            a += 1
        else:
            z -= 1

    c0, c1 = (a, z) if a > z else (z, a)
    p0, p1 = unpack565(c0), unpack565(c1)

    pal = np.stack(
        [
            p0,
            p1,
            (2 * p0 + p1) // 3,
            (p0 + 2 * p1) // 3,
        ]
    )

    ids = ((px[:, None, :] - pal[None, :, :]) ** 2).sum(2).argmin(1)
    ids = ids.reshape(4, 4)

    selectors = bytes(
        sum((int(ids[y, x]) & 3) << (6 - 2 * x) for x in range(4))
        for y in range(4)
    )

    return struct.pack(">HH", c0, c1) + selectors


def encode_cmpr_level(img: Image.Image):
    ar = np.asarray(img.convert("RGBA"))
    h, w = ar.shape[:2]

    ph = max(8, ((h + 7) // 8) * 8)
    pw = max(8, ((w + 7) // 8) * 8)

    pad = np.empty((ph, pw, 4), dtype=np.uint8)
    pad[:h, :w] = ar

    if pw > w:
        pad[:h, w:] = ar[:h, w - 1:w]
    if ph > h:
        pad[h:] = pad[h - 1:h]

    out = bytearray()

    for y in range(0, ph, 8):
        for x in range(0, pw, 8):
            tile = pad[y:y + 8, x:x + 8]
            for sy in range(2):
                for sx in range(2):
                    out += encode_dxt1_block(
                        tile[sy * 4:(sy + 1) * 4, sx * 4:(sx + 1) * 4]
                    )

    align32(out)
    return bytes(out)


def encode_cmpr_mips(atlas: Image.Image, levels: int):
    out = bytearray()
    for i in range(levels):
        w = max(1, atlas.width >> i)
        h = max(1, atlas.height >> i)
        cur = atlas.resize((w, h), Image.Resampling.LANCZOS)
        out += encode_cmpr_level(cur)
    return bytes(out)



# ---------------------------------------------------------------------------
# Wii RGB5A3 encoder (fmt=2)
# ---------------------------------------------------------------------------

def encode_rgb5a3(atlas: Image.Image):
    """Encode PIL RGBA image into native GX RGB5A3 4x4 tiled order.

    This is the same packing used by SSRCTexturesEditor RGB5A3_FIX:
      * alpha >= 224 -> opaque RGB555 (1RRRRRGGGGGBBBBB)
      * otherwise    -> A3RGB4       (0AAARRRRGGGGBBBB)
    """
    rgba = np.asarray(atlas.convert("RGBA"), dtype=np.uint8)
    h, w = rgba.shape[:2]
    out = bytearray()

    def pack_pixel(r, g, b, a):
        if int(a) >= 224:
            rr = (int(r) * 31 + 127) // 255
            gg = (int(g) * 31 + 127) // 255
            bb = (int(b) * 31 + 127) // 255
            return 0x8000 | (rr << 10) | (gg << 5) | bb
        aa = (int(a) * 7 + 127) // 255
        rr = (int(r) * 15 + 127) // 255
        gg = (int(g) * 15 + 127) // 255
        bb = (int(b) * 15 + 127) // 255
        return (aa << 12) | (rr << 8) | (gg << 4) | bb

    for ty in range(0, h, 4):
        for tx in range(0, w, 4):
            for yy in range(4):
                for xx in range(4):
                    x, y = tx + xx, ty + yy
                    if x < w and y < h:
                        r, g, b, a = rgba[y, x]
                    else:
                        r = g = b = a = 0
                    out += struct.pack(">H", pack_pixel(r, g, b, a))
    return bytes(out)


def _top_tree_chunks(roots):
    out = []
    for r in roots:
        if r.type == 0x0001 and r.has_children:
            out.extend(r.children)
        else:
            out.append(r)
    return out


def _direct_child_tree(c, type_id):
    return next((x for x in c.children if x.type == type_id), None)


def _tree_asset_info(c):
    h = _direct_child_tree(c, 0x138E)
    if h is None or len(h.data) < 92:
        return None
    tex_hash, atype = struct.unpack_from(">II", h.data, 0)
    name = h.data[28:92].split(b"\\x00", 1)[0].decode("latin1", errors="replace")
    return tex_hash, atype, name


class _TextureRepackChunk:
    __slots__ = ('raw_id','type','version','has_children','reserved','data','children','tail','src_off')

    def __init__(self, raw_id, version, has_children, reserved, src_off=None):
        self.raw_id = raw_id
        self.type = raw_id & 0x7FFFFFFF
        self.version = version
        self.has_children = has_children
        self.reserved = reserved
        self.data = b''
        self.children = []
        self.tail = b''
        self.src_off = src_off

    @staticmethod
    def parse_stream(buf, start, end):
        chunks = []
        p = start
        while p + 16 <= end:
            raw_id, ver, has_children, reserved, size = struct.unpack_from(">IHHII", buf, p)
            if not (raw_id & 0x80000000) or p + 16 + size > end:
                break
            c = _TextureRepackChunk(raw_id, ver, has_children, reserved, p)
            ps, pe = p + 16, p + 16 + size
            if has_children:
                c.children, c.tail = _TextureRepackChunk.parse_stream(buf, ps, pe)
            else:
                c.data = bytes(buf[ps:pe])
            chunks.append(c)
            p = pe
        return chunks, bytes(buf[p:end])

    def payload_size(self):
        if self.has_children:
            return sum(x.total_size() for x in self.children) + len(self.tail)
        return len(self.data)

    def total_size(self):
        return 16 + self.payload_size()

    def serialize(self, out):
        out += struct.pack(">IHHII", self.raw_id, self.version,
                           self.has_children, self.reserved, self.payload_size())
        if self.has_children:
            for x in self.children:
                x.serialize(out)
            out += self.tail
        else:
            out += self.data


def _texture_repack_roots(buf):
    return _TextureRepackChunk.parse_stream(buf, 0, len(buf))


def _texture_repack_serialize(roots, tail=b''):
    out = bytearray()
    for c in roots:
        c.serialize(out)
    out += tail
    return bytes(out)


def _tex_direct_child(c, type_id):
    return next((x for x in c.children if x.type == type_id), None)


def _tex_top_chunks(roots):
    out = []
    for r in roots:
        if r.type == 0x0001 and r.has_children:
            out.extend(r.children)
        else:
            out.append(r)
    return out


def _tex_asset_info(c):
    h = _tex_direct_child(c, 0x138E)
    if h is None or len(h.data) < 92:
        return None
    tex_hash, atype = struct.unpack_from(">II", h.data, 0)
    name = h.data[28:92].split(b"\x00", 1)[0].decode("latin1", errors="replace")
    return tex_hash, atype, name


def replace_texture_rgb5a3_variable_size(buf: bytes, target_name: str,
                                         atlas: Image.Image):
    """Replace a texture with RGB5A3 using the exact generic PKZ repacker
    proven by SSRCTexturesEditor.  Do not rely on the model-oriented Goliath
    parser here because some texture 0x0026 assets are not represented by that
    tree in the same way."""
    # Resolve the authoritative texture hash from the registry first.
    reg = sklib.texture_registry(buf)
    info = reg.get('byname', {}).get(target_name.lower())
    if info is None:
        names = sorted(x.get('name','') for x in reg.get('byhash', {}).values())
        raise ValueError(f'Texture "{target_name}" is not in registry. Available: {names}')
    target_hash = int(info['hash'])

    roots, tail = _texture_repack_roots(buf)
    tops = _tex_top_chunks(roots)

    payload_chunk = None
    payload_name = None
    for c in tops:
        if c.type != 0x0026 or not c.has_children:
            continue
        ai = _tex_asset_info(c)
        if not ai:
            continue
        h, atype, name = ai
        if atype == 4 and int(h) == target_hash:
            payload_chunk = _tex_direct_child(c, 0x0195)
            payload_name = name
            break

    if payload_chunk is None:
        # Diagnostic fallback: show type-4 assets actually found by hash/name.
        found = []
        for c in tops:
            if c.type == 0x0026 and c.has_children:
                ai = _tex_asset_info(c)
                if ai and ai[1] == 4:
                    found.append(f'{ai[2]}=0x{ai[0]:08X}')
        raise ValueError(
            f'Could not find texture payload for "{target_name}" '
            f'(hash 0x{target_hash:08X}) / 0x0195. Type-4 assets: {found}'
        )

    payload = encode_rgb5a3(atlas)
    payload_chunk.data = payload

    meta_found = False
    for c in tops:
        if c.type != 0x0009 or not c.has_children:
            continue
        for d in c.children:
            if d.type != 0x138D or not d.has_children:
                continue
            h138e = _tex_direct_child(d, 0x138E)
            c191 = _tex_direct_child(d, 0x0191)
            if h138e is None or c191 is None or len(h138e.data) < 8:
                continue
            h, atype = struct.unpack_from(">II", h138e.data, 0)
            if int(h) != target_hash:
                continue
            c197 = _tex_direct_child(c191, 0x0197)
            if c197 is None or len(c197.data) < 20:
                raise ValueError("Matching texture registry entry has no valid 0x0197")
            md = bytearray(c197.data)
            struct.pack_into(">5I", md, 0,
                             int(atlas.height), int(atlas.width),
                             1, 2, len(payload))
            c197.data = bytes(md)
            meta_found = True
            break
        if meta_found:
            break

    if not meta_found:
        raise ValueError(f'Could not update 0x0197 metadata for "{target_name}"')

    repacked = _texture_repack_serialize(roots, tail)
    repacked, repairs = repair_absolute_asset_refs(buf, repacked)

    # Verify both registry metadata and physical payload after repack.
    reg2 = sklib.texture_registry(repacked)
    chk = reg2.get('byhash', {}).get(target_hash)
    pays = sklib.texture_payloads(repacked)
    if chk is None or target_hash not in pays:
        raise ValueError('RGB5A3 repack verification failed: texture disappeared')
    if int(chk['w']) != atlas.width or int(chk['h']) != atlas.height or int(chk['fmt']) != 2:
        raise ValueError(f'RGB5A3 metadata verification failed: {chk}')
    _, _, plen = pays[target_hash]
    if int(plen) != len(payload):
        raise ValueError(f'RGB5A3 payload verification failed: {plen} != {len(payload)}')

    print(f'Texture asset: {payload_name} / hash=0x{target_hash:08X}')
    return repacked, repairs, len(payload)

# ---------------------------------------------------------------------------
# PKZ directory offset repair
# ---------------------------------------------------------------------------

def repair_absolute_asset_refs(original: bytes, rebuilt: bytes):
    old_assets = sorted(
        (off, name, atype, h, size)
        for name, atype, h, off, size, kids in sklib.assets(original)
    )
    new_assets = sorted(
        (off, name, atype, h, size)
        for name, atype, h, off, size, kids in sklib.assets(rebuilt)
    )

    new_by_key = {
        (name, atype, h): off
        for off, name, atype, h, size in new_assets
    }

    fixed = bytearray(rebuilt)
    first_asset = min(x[0] for x in new_assets)
    repairs = 0

    for oldoff, name, atype, h, size in old_assets:
        newoff = new_by_key.get((name, atype, h))
        if newoff is None or newoff == oldoff:
            continue

        pat = struct.pack(">I", oldoff)
        pos = 0

        while True:
            q = fixed.find(pat, pos, first_asset)
            if q < 0:
                break
            fixed[q:q + 4] = struct.pack(">I", newoff)
            repairs += 1
            pos = q + 4

    return bytes(fixed), repairs


# ---------------------------------------------------------------------------
# Texture selection
# ---------------------------------------------------------------------------

def choose_target_texture(buf: bytes, asset_children):
    """
    Prefer the first texture hash referenced by the model material and resolve
    it through the texture registry.

    This correctly found:
      FireCar_SC_D
      KoopaPlane_SC_D
      LifeCopter_SC_C
    in our tests.
    """
    mats = sklib.model_materials(buf, asset_children)
    if not mats:
        raise ValueError("Target model has no parsed materials")

    hashes = []
    for m in mats:
        for h, path in m.get("textures", []):
            hashes.append(int(h))

    if not hashes:
        raise ValueError("Target material has no texture references")

    reg = sklib.texture_registry(buf)
    byhash = {}

    for info in reg["byname"].values():
        byhash.setdefault(int(info["hash"]), info)

    for h in hashes:
        info = byhash.get(h)
        if info and info["fmt"] == 3:
            return info["name"]

    # Fall back to first resolved texture.
    for h in hashes:
        info = byhash.get(h)
        if info:
            return info["name"]

    raise ValueError("Could not resolve target material texture")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_static(
    pkz_path,
    custom_glb_path,
    out_path,
    model_name=None,
    target_glb_path=None,
    atlas_out=None,
    no_fit=False,
):
    original = Path(pkz_path).read_bytes()

    asset = choose_model(original, model_name)
    model = sk_geo.parse_model(original, asset[5])
    layout_kind = validate_target_layout(original, model)

    opcode = detect_target_vat_opcode(original, model)
    joint = dominant_rigid_joint(original, model)

    print(f"Target model : {asset[0]}")
    print(f"LODs         : {len(model['lods'])}")
    print(f"GX opcode    : 0x{opcode:02X}")
    print(f"Rigid joint  : {joint}")
    print(f"Layout       : {layout_kind}")

    # ---------------- custom GLB
    g, b = gltf.load_glb(custom_glb_path)

    if not g.get("meshes"):
        raise ValueError("Custom GLB contains no meshes")

    material_ids = []
    parts = []

    for mi, mesh in enumerate(g["meshes"]):
        for pi, primitive in enumerate(mesh.get("primitives", [])):
            q = gltf.transform_primitive_to_mesh_space(g, b, mi, pi, 0)

            key, chosen_uv = choose_uv_attribute(g, b, primitive)
            if len(chosen_uv) == 0:
                chosen_uv = np.zeros((len(q["P"]), 2), dtype=np.float64)

            mat = int(primitive.get("material", 0))
            material_ids.append(mat)

            parts.append(
                {
                    "P": q["P"].astype(np.float64),
                    "N": q["N"].astype(np.float64),
                    "UV": chosen_uv.astype(np.float64),
                    "I": q["I"].astype(np.int64),
                    "mat": mat,
                    "uv_key": key,
                }
            )

    # ---------------- orientation auto-detect
    # The stable importer intentionally keeps mesh 0 in local GLB space because
    # Blender often stores a cosmetic root axis-conversion. Some downloaded GLBs
    # (e.g. Sketchfab exports) instead rely on that root transform for the model's
    # real upright orientation. Compare local-vs-root-baked axis proportions to
    # the original target GLB and only bake the root when it is a clearly better
    # match. This preserves the old behavior for already-working vehicles.
    orientation_mode = "local"
    if target_glb_path and parts:
        tg_or, tb_or = gltf.load_glb(target_glb_path)
        tmin_or, tmax_or = glb_bounds(tg_or, tb_or)
        tsize_or = np.maximum(tmax_or - tmin_or, 1e-12)

        P_local = np.concatenate([p["P"] for p in parts], axis=0)
        Mroot = _mesh_world(g, 0)
        P4 = np.concatenate([P_local, np.ones((len(P_local), 1))], axis=1)
        P_world = (P4 @ Mroot.T)[:, :3]

        def _axis_shape_score(Pcand):
            size = np.maximum(Pcand.max(0) - Pcand.min(0), 1e-12)
            a = size / np.max(size)
            t = tsize_or / np.max(tsize_or)
            return float(np.sum((np.log(a) - np.log(t)) ** 2))

        local_score = _axis_shape_score(P_local)
        world_score = _axis_shape_score(P_world)

        # Require a meaningful improvement so harmless Blender root transforms
        # do not unexpectedly change models that already import correctly.
        if world_score + 0.10 < local_score:
            try:
                NM = np.linalg.inv(Mroot[:3, :3]).T
            except np.linalg.LinAlgError:
                NM = Mroot[:3, :3]
            for p in parts:
                Ph = np.concatenate([p["P"], np.ones((len(p["P"]), 1))], axis=1)
                p["P"] = (Ph @ Mroot.T)[:, :3]
                N2 = p["N"] @ NM.T
                L = np.linalg.norm(N2, axis=1)
                L[L < 1e-20] = 1.0
                p["N"] = N2 / L[:, None]
            orientation_mode = "root/world"

        print(f"Orientation scores: local={local_score:.4f} root/world={world_score:.4f} -> {orientation_mode}")

    # ---------------- atlas
    atlas, rects, uv_domains = build_repeat_safe_atlas(g, b, parts, 512)

    if atlas_out:
        Path(atlas_out).parent.mkdir(parents=True, exist_ok=True)
        atlas.save(atlas_out)

    # ---------------- merge
    Pparts, Nparts, UVparts, Iparts = [], [], [], []
    base = 0

    for p in parts:
        uv = p["UV"].copy()

        # Preserve repeating UV interpolation.  Do NOT apply mod(uv,1): that
        # creates discontinuities inside triangles crossing an integer seam.
        du0, dv0, dus, dvs = uv_domains[p["mat"]]
        uv[:, 0] = (uv[:, 0] - du0) / dus
        uv[:, 1] = (uv[:, 1] - dv0) / dvs

        u0, v0, us, vs = rects[p["mat"]]
        uv[:, 0] = u0 + uv[:, 0] * us
        uv[:, 1] = v0 + uv[:, 1] * vs

        Pparts.append(p["P"])
        Nparts.append(p["N"])
        UVparts.append(uv)
        Iparts.append(p["I"] + base)

        print(
            f'  part mat={p["mat"]} UV={p["uv_key"]} '
            f'verts={len(p["P"])} tris={len(p["I"])//3}'
        )

        base += len(p["P"])

    P = np.concatenate(Pparts, axis=0)
    N = np.concatenate(Nparts, axis=0)
    UV = np.concatenate(UVparts, axis=0)
    I = np.concatenate(Iparts, axis=0)

    print(f"Custom total : {len(P)} verts / {len(I)//3} tris")

    if len(P) >= 65536:
        raise ValueError(
            f"Custom model has {len(P)} physical vertices. "
            "Current GX writer uses u16 indices; simplify the model below 65536."
        )

    # ---------------- fit to target GLB
    if target_glb_path and not no_fit:
        tg, tb = gltf.load_glb(target_glb_path)
        tmin, tmax = glb_bounds(tg, tb)

        pmin = P.min(0)
        pmax = P.max(0)
        psize = pmax - pmin
        tsize = tmax - tmin

        safe = psize > 1e-8
        ratios = tsize[safe] / psize[safe]
        scale = 0.88 * float(np.min(ratios)) if len(ratios) else 1.0

        pcenter = (pmin + pmax) * 0.5
        tcenter = (tmin + tmax) * 0.5

        P = (P - pcenter) * scale + tcenter

        # Put bottom on target floor.
        P[:, 1] += tmin[1] - P[:, 1].min()

        print(f"Auto fit     : scale={scale:.6f}")
        print(f"Orientation  : {orientation_mode} GLB mapping")

    # ---------------- chunk tree
    roots, tail = gp.parse_package(original, endian=">")

    byoff = {}

    def walk(c):
        byoff[c.src_off] = c
        for ch in c.children:
            walk(ch)

    for r in roots:
        walk(r)

    asset_chunk = byoff[asset[3]]

    # ---------------- descriptor mapping
    # Rebuilt using the mapping strategy from sk_import_gltf_test.py: descriptors
    # belong to a specific 0x0325 descriptor block / LOD.  Do not identify them
    # globally by their byte signature: several vehicle LODs can be identical.
    descriptor_blocks = []
    for c325 in asset_chunk.children:
        if c325.type != 0x0325:
            continue
        ds = [ch for ch in c325.children if ch.type == 0x0327]
        if ds:
            descriptor_blocks.append(ds)

    if len(descriptor_blocks) != len(model["lods"]):
        raise ValueError(
            f"Descriptor block / LOD mismatch: blocks={len(descriptor_blocks)} "
            f"lods={len(model['lods'])}"
        )

    for li, (block, L) in enumerate(zip(descriptor_blocks, model["lods"])):
        if len(block) != len(L["descs"]):
            raise ValueError(
                f"LOD {li}: descriptor count mismatch: tree={len(block)} "
                f"parsed={len(L['descs'])}"
            )

    # ---------------- 1771
    p1771 = bytearray()

    for p, n in zip(P, N):
        ln = np.linalg.norm(n)
        nn = n / (ln if ln > 1e-12 else 1.0)

        nq = np.clip(
            np.rint(nn * 64.0),
            -128,
            127,
        ).astype(int)

        p1771 += struct.pack(">3f", *map(float, p))
        p1771 += bytes(
            (
                nq[0] & 255,
                nq[1] & 255,
                nq[2] & 255,
                0,
            )
        )

    # ---------------- 1770
    p1770 = (
        struct.pack(">5I", len(P), 0, 1, len(P), joint)
        + struct.pack(">I", len(P))
    )

    # ---------------- 1388
    arena1388 = bytearray()

    for uv in UV:
        quv = np.clip(
            np.rint(uv * 1024.0),
            -32768,
            32767,
        ).astype(int)

        if layout_kind == "static16":
            # Mod_SC layout: RGBA/mode + UV + 4 joints + 4 weights.
            arena1388 += bytes((255, 255, 255, 0x19))
            arena1388 += struct.pack(
                ">2h",
                int(quv[0]),
                int(quv[1]),
            )
        else:
            # FireCar / FireCar_Dark layout: UV directly at offset 0.
            arena1388 += struct.pack(
                ">2h",
                int(quv[0]),
                int(quv[1]),
            )

        # Rigidly attach the imported static custom geometry to the target's
        # dominant vehicle joint.  stride12 stores this immediately after UV;
        # stride16 stores it after color+UV.
        arena1388 += bytes(
            (
                joint & 255,
                0,
                0,
                0,
                255,
                0,
                0,
                0,
            )
        )

    p1388 = struct.pack(">I", len(arena1388)) + bytes(arena1388)

    # ---------------- 1389
    # Keep the target's VAT selection (normally 0x9B / VAT3).
    dl = bytearray()

    for tri in I.reshape(-1, 3):
        dl.append(opcode)
        dl += struct.pack(">H", 3)

        for vi in map(int, tri):
            if layout_kind == "static16":
                # pos, nrm, color, uv
                dl += struct.pack(">4H", vi, vi, vi, vi)
            else:
                # stride12 FireCar/Dark: pos, nrm, uv
                dl += struct.pack(">3H", vi, vi, vi)

    align32(dl)
    p1389 = struct.pack(">I", len(dl)) + bytes(dl)

    # ---------------- all LODs
    # Each LOD is paired directly with its own descriptor block.  This is the
    # important fix for FireCar: all four LOD descriptors may be byte-identical.
    for li, L in enumerate(model["lods"]):
        geo = L["geo"]
        if len(L["descs"]) != 1 or len(descriptor_blocks[li]) != 1:
            raise ValueError(
                f"LOD {li}: this static vehicle path currently requires exactly "
                "one descriptor"
            )

        byoff[geo[0x1771][0] - 16].data = bytes(p1771)
        byoff[geo[0x1770][0] - 16].data = bytes(p1770)
        byoff[geo[0x1388][0] - 16].data = bytes(p1388)
        byoff[geo[0x1389][0] - 16].data = bytes(p1389)

        c = descriptor_blocks[li][0]
        if len(c.data) < 56:
            raise ValueError(f"LOD {li}: descriptor payload too short")
        u = list(struct.unpack_from(">14I", c.data, 0))
        u[1] = 0
        u[4] = len(P)
        u[5] = 0
        u[6] = len(dl)
        dat = bytearray(c.data)
        struct.pack_into(">14I", dat, 0, *u)
        c.data = bytes(dat)
        print(f"  LOD {li}: direct descriptor block mapping OK")

    rebuilt = gp.serialize_package(roots, tail)
    rebuilt, repairs = repair_absolute_asset_refs(original, rebuilt)

    # ---------------- texture (512x512 RGB5A3, variable-size repack)
    target_texture = choose_target_texture(rebuilt, asset[5])

    # Keep the packed 512x512 atlas exactly as generated. RGB5A3 is uncompressed
    # 16-bit GX color and avoids the CMPR/DXT1 block artefacts visible on logos.
    if atlas.size != (512, 512):
        atlas = atlas.resize((512, 512), Image.Resampling.LANCZOS)

    rebuilt_tex, tex_repairs, payload_len = replace_texture_rgb5a3_variable_size(
        rebuilt, target_texture, atlas
    )
    repairs += tex_repairs
    outbuf = bytearray(rebuilt_tex)

    print(f"Texture mode : RGB5A3 fmt=2 / 512x512 / 1 mip")
    print(f"Texture bytes: {payload_len}")

    Path(out_path).write_bytes(outbuf)

    # ---------------- sanity
    test = bytes(outbuf)
    ta = next(
        a
        for a in sklib.assets(test)
        if a[0].lower() == asset[0].lower()
        and a[1] == 3
    )

    tm = sk_geo.parse_model(test, ta[5])

    for li, L in enumerate(tm["lods"]):
        pp, _ = sk_geo.read_1771(test, L["geo"])
        lay = sk_geo.solve_layout(
            test,
            L["geo"],
            L["descs"][0],
        )
        ops = sk_geo.parse_dl(
            test,
            L["geo"],
            L["descs"][0],
            lay,
            len(pp),
        )
        tris = sk_geo.dl_triangles(ops)

        print(
            f"LOD{li}: {len(pp)} verts / "
            f"{len(tris)} tris / {len(ops)} GX commands"
        )

    print("")
    print("=== SUCCESS ===")
    print(f"Model        : {asset[0]}")
    print(f"Texture      : {target_texture}")
    print(f"GX opcode    : 0x{opcode:02X}")
    print(f"Vertices     : {len(P)}")
    print(f"Triangles    : {len(I)//3}")
    print(f"Refs repaired: {repairs}")
    print(f"Output       : {out_path}")


# ---------------------------------------------------------------------------
# GUI / CLI
# ---------------------------------------------------------------------------

def run_gui():
    import sys
    import traceback
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("Skylanders Static Vehicle Importer")
    root.geometry("760x500")
    root.minsize(680, 430)

    pkz_var = tk.StringVar()
    target_var = tk.StringVar()
    custom_var = tk.StringVar()

    frm = tk.Frame(root, padx=14, pady=14)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    title = tk.Label(
        frm,
        text="Skylanders SuperChargers Racing Wii - Custom Vehicle Importer",
        font=("Segoe UI", 13, "bold"),
    )
    title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    info = tk.Label(
        frm,
        text=(
            "Select the original PKZ, the original GLB exported from the vehicle, "
            "and your custom GLB. The internal vehicle model is detected automatically."
        ),
        justify="left",
        wraplength=700,
    )
    info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))

    def choose_file(var, filetypes):
        fn = filedialog.askopenfilename(filetypes=filetypes)
        if fn:
            var.set(fn)

    rows = [
        ("Original PKZ", pkz_var, [("PKZ files", "*.pkz"), ("All files", "*.*")]),
        ("Original GLB", target_var, [("GLB files", "*.glb"), ("All files", "*.*")]),
        ("Custom GLB", custom_var, [("GLB files", "*.glb"), ("All files", "*.*")]),
    ]

    for i, (label, var, ftypes) in enumerate(rows, start=2):
        tk.Label(frm, text=label + " :").grid(row=i, column=0, sticky="w", padx=(0, 8), pady=5)
        tk.Entry(frm, textvariable=var).grid(row=i, column=1, sticky="ew", pady=5)
        tk.Button(
            frm,
            text="Browse...",
            command=lambda v=var, ft=ftypes: choose_file(v, ft),
            width=12,
        ).grid(row=i, column=2, padx=(8, 0), pady=5)

    log = ScrolledText(frm, height=14, state="disabled", font=("Consolas", 9))
    log.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(14, 8))
    frm.rowconfigure(6, weight=1)

    def log_write(text):
        def _do():
            log.configure(state="normal")
            log.insert("end", text)
            log.see("end")
            log.configure(state="disabled")
        root.after(0, _do)

    class GuiWriter:
        def write(self, s):
            if s:
                log_write(s)
        def flush(self):
            pass

    import_btn = tk.Button(frm, text="IMPORT", font=("Segoe UI", 11, "bold"), height=2)
    import_btn.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))

    def do_import():
        pkz = pkz_var.get().strip()
        target = target_var.get().strip()
        custom = custom_var.get().strip()

        missing = []
        if not pkz or not Path(pkz).is_file():
            missing.append("Original PKZ")
        if not target or not Path(target).is_file():
            missing.append("Original GLB")
        if not custom or not Path(custom).is_file():
            missing.append("Custom GLB")

        if missing:
            messagebox.showerror("Missing file", "Select: " + ", ".join(missing))
            return

        pkz_p = Path(pkz)
        custom_p = Path(custom)
        out = pkz_p.with_name(pkz_p.stem + "_CUSTOM.pkz")
        atlas = pkz_p.with_name(pkz_p.stem + "_CUSTOM_atlas.png")

        import_btn.configure(state="disabled", text="IMPORTING...")
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")

        log_write(f"PKZ      : {pkz}\n")
        log_write(f"Original: {target}\n")
        log_write(f"Custom   : {custom}\n")
        log_write(f"Output   : {out}\n\n")

        def worker():
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = GuiWriter()
            sys.stderr = GuiWriter()
            try:
                # Prefer the model encoded in exporter filenames such as
                # 3224_FireCar_Mod_0_AD__FireCar_Dark.glb.  This is important
                # when one PKZ contains both FireCar and FireCar_Dark.
                wanted_model = None
                stem = Path(target).stem
                if "__" in stem:
                    candidate = stem.rsplit("__", 1)[1]
                    names = {a[0].lower(): a[0] for a in sklib.assets(Path(pkz).read_bytes()) if a[1] == 3}
                    wanted_model = names.get(candidate.lower())

                import_static(
                    pkz_path=pkz,
                    custom_glb_path=custom,
                    out_path=str(out),
                    model_name=wanted_model,
                    target_glb_path=target,
                    atlas_out=str(atlas),
                    no_fit=False,
                )
                root.after(0, lambda: messagebox.showinfo(
                    "Completed",
                    "Import successful!\n\nCreated file:\n" + str(out),
                ))
            except Exception as e:
                traceback.print_exc()
                root.after(0, lambda err=str(e): messagebox.showerror(
                    "Import error",
                    err + "\n\nThe full error details are shown in the log window.",
                ))
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                root.after(0, lambda: import_btn.configure(state="normal", text="IMPORT"))

        threading.Thread(target=worker, daemon=True).start()

    import_btn.configure(command=do_import)
    root.mainloop()


def run_cli():
    ap = argparse.ArgumentParser(
        description="Experimental Skylanders Wii static Model Fix importer"
    )
    ap.add_argument("--pkz", required=True, help="Original vehicle PKZ")
    ap.add_argument("--custom", required=True, help="Custom GLB to import")
    ap.add_argument("--out", required=True, help="Output PKZ")
    ap.add_argument(
        "--model",
        help="Target model asset name. If omitted, auto-detects a compatible *_SC model.",
    )
    ap.add_argument(
        "--target-glb",
        help="Exported original target Model Fix GLB used for automatic fitting.",
    )
    ap.add_argument("--atlas-out", help="Optional PNG path for the generated atlas")
    ap.add_argument("--no-fit", action="store_true", help="Disable automatic fitting")
    args = ap.parse_args()

    import_static(
        pkz_path=args.pkz,
        custom_glb_path=args.custom,
        out_path=args.out,
        model_name=args.model,
        target_glb_path=args.target_glb,
        atlas_out=args.atlas_out,
        no_fit=args.no_fit,
    )


def main():
    # Double-click / no arguments => simple 3-file GUI.
    # Existing command-line workflow is still supported when arguments are supplied.
    import sys
    if len(sys.argv) == 1:
        run_gui()
    else:
        run_cli()


if __name__ == "__main__":
    main()
