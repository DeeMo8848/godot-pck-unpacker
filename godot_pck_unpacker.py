#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Godot PCK Unpacker  ——  通用 Godot 资源包解包工具
=================================================

支持的 PCK 格式版本:
  - v1 (Godot 3.x)        : 尽力支持 (header 84字节, 绝对偏移, 条目无 flags)
  - v2 (Godot 4.0-4.4)    : 完整支持 (已实测 Godot 4.4.1)
  - v3 (Godot 4.5+)       : 完整支持 (新增 DirectoryOffset 字段)

功能:
  - 自动识别 PCK 版本 (读 FormatVersion)
  - 解包全部文件, 保留 res:// 目录结构
  - 纹理 (Godot4 .ctex / Godot3 .stex) 自动转成 PNG (或 WebP 回退)
  - 音频 (.sample) 自动还原成可播放的 .wav
  - 字体 (.fontdata) 自动还原成可安装的 .ttf/.otf
  - 利用 .import 映射把纹理/音频/字体归位到开发时的原始路径 (干净文件名)
  - 加密文件自动跳过并告警

依赖:
  - 仅需 Python 3 (标准库)
  - 可选: Pillow     (用于把 WebP 转成 PNG; 没有则直接保存 .webp)
  - 可选: zstandard  (用于解压 .fontdata; 没有则跳过字体转换, 保留原始文件)

用法:
  python godot_pck_unpacker.py game.pck
  python godot_pck_unpacker.py game.pck -o out_dir
  python godot_pck_unpacker.py game.pck --list          # 仅列出文件
  python godot_pck_unpacker.py game.pck --no-convert    # 不转纹理
  python godot_pck_unpacker.py game.pck --no-organize   # 不按原始路径归位
"""

import os
import sys
import re
import struct
import argparse

# ---------- 常量 ----------
MAGIC = b"GDPC"
RES_PREFIX = "res://"
PCK_FILE_ENCRYPTED = 1 << 0
PCK_FILE_DELETED = 1 << 1


def eprint(*a):
    print(*a, file=sys.stderr)


# ---------- Header 解析 ----------
def parse_header(f):
    """读取并解析 PCK 文件头。返回 header 字典。"""
    f.seek(0)
    magic = f.read(4)
    if magic != MAGIC:
        raise ValueError(
            "不是 Godot PCK 文件 (魔数=%r, 应为 %r)。\n"
            "提示: 极老的 Godot 1/2 用 'PCKG' 魔数, 本工具不支持。"
            % (magic, MAGIC)
        )

    version = struct.unpack("<I", f.read(4))[0]
    major = struct.unpack("<I", f.read(4))[0]
    minor = struct.unpack("<I", f.read(4))[0]
    patch = struct.unpack("<I", f.read(4))[0]

    # 版本相关字段
    if version >= 2:
        flags = struct.unpack("<I", f.read(4))[0]
        fob = struct.unpack("<Q", f.read(8))[0]      # FileOffsetBase
    else:
        flags = 0
        fob = 0

    if version >= 3:
        dir_off = struct.unpack("<Q", f.read(8))[0]  # DirectoryOffset
    else:
        dir_off = 0

    # 跳过保留区到达 header 末尾
    if version >= 3:
        f.read(64)
        header_size = 104
    elif version >= 2:
        f.read(64)
        header_size = 96
    else:
        f.read(64)
        header_size = 84

    return {
        "version": version,
        "major": major,
        "minor": minor,
        "patch": patch,
        "flags": flags,
        "fob": fob,
        "dir_off": dir_off,
        "header_size": header_size,
    }


def directory_offset(hdr):
    """计算目录区起始偏移。"""
    if hdr["version"] >= 3 and hdr["dir_off"] != 0:
        return hdr["dir_off"]
    return hdr["header_size"]


def parse_directory(f, hdr, fsize):
    """解析目录区, 返回 entry 列表。"""
    doff = directory_offset(hdr)
    f.seek(doff)
    file_count = struct.unpack("<I", f.read(4))[0]

    if file_count == 0 or file_count > 5_000_000:
        raise ValueError(
            "文件数量异常 (%d), 可能是版本识别错误或文件损坏。\n"
            "header version=%d, dir_off=0x%X"
            % (file_count, hdr["version"], doff)
        )

    entries = []
    for i in range(file_count):
        path_len = struct.unpack("<I", f.read(4))[0]
        if path_len <= 0 or path_len > 8192:
            raise ValueError("第 %d 个条目 path_len 异常: %d" % (i, path_len))
        padded = (path_len + 3) & ~3
        raw = f.read(padded)
        # 解码并清洗: 去掉嵌入/尾随的 NUL 与空白 (Godot 部分条目的 path_len 含 NUL 终止符)
        path = raw[:path_len].decode("utf-8", errors="replace")
        path = path.replace("\x00", "").strip()

        offset = struct.unpack("<Q", f.read(8))[0]
        size = struct.unpack("<Q", f.read(8))[0]
        md5 = f.read(16)

        if hdr["version"] >= 2:
            flags_val = struct.unpack("<I", f.read(4))[0]
        else:
            flags_val = 0

        # 偏移计算
        if hdr["version"] >= 2:
            abs_offset = hdr["fob"] + offset
        else:
            abs_offset = offset  # v1 为绝对偏移

        entries.append({
            "path": path,
            "abs_offset": abs_offset,
            "size": size,
            "flags": flags_val,
        })

    return entries


# ---------- .import 映射 ----------
def build_import_map(f, entries, fsize):
    """扫描 .import / .remap 条目, 建立 导入产物文件名 -> 原始资源路径 的映射。

    Godot 4 工程通常用 .import 记录导入元数据; 部分配置/导出产物改用 .remap。
    二者都是纯文本元数据(约 1KB)。真正产物在 res://.godot/imported/ 下:
      纹理 foo.png-<hash>.ctex / 音频 foo.wav-<hash>.sample / 字体 foo.ttf-<hash>.fontdata
    映射后即可把产物还原成开发时的原始路径与文件名。
    """
    imp_map = {}
    # .import: 形如  path="res://.godot/imported/foo.png-<hash>.ctex"
    pat_import = re.compile(r'path="(res://\.godot/imported/[^"]+)"')
    # .remap:  形如  loaded_paths=["res://.godot/imported/..."]
    #             或    path="res://.godot/imported/..."
    pat_remap = re.compile(
        r'(?:loaded_paths=\[|path=)"?(res://\.godot/imported/[^"\]]+)"?')
    for ent in entries:
        path = ent["path"]
        if path.endswith(".import"):
            suf, pat = ".import", pat_import
        elif path.endswith(".remap"):
            suf, pat = ".remap", pat_remap
        else:
            continue
        if ent["abs_offset"] + ent["size"] > fsize:
            continue
        f.seek(ent["abs_offset"])
        try:
            content = f.read(ent["size"]).decode("utf-8", errors="ignore")
        except Exception:
            continue
        m = pat.search(content)
        if m:
            dest_name = m.group(1).rsplit("/", 1)[-1]
            orig = path[: -len(suf)]
            imp_map[dest_name] = orig
    return imp_map


# ---------- 纹理提取 ----------
def find_embedded_image(data):
    """在纹理二进制中查找内嵌的 WebP / PNG, 返回 (bytes, fmt)。"""
    # WebP (RIFF....WEBP)
    i = 0
    n = len(data)
    while i + 12 <= n:
        if data[i:i + 4] == b"RIFF" and data[i + 8:i + 12] == b"WEBP":
            size = struct.unpack_from("<I", data, i + 4)[0]
            end = i + 8 + size
            if end <= n:
                return data[i:end], "WEBP"
        i += 1
    # PNG
    p = data.find(b"\x89PNG\r\n\x1a\n")
    if p >= 0:
        iend = data.find(b"IEND", p)
        if iend >= 0:
            return data[p:iend + 8], "PNG"
    return None, None


def convert_texture(data, ext):
    """把纹理二进制转成可保存的图像 (bytes, fmt)。失败返回 (None, None)。"""
    if ext == ".ctex":
        return find_embedded_image(data)
    elif ext == ".stex":
        # Godot 3: 尽力找内嵌 WebP/PNG
        return find_embedded_image(data)
    return None, None


def _ctex_basename(ctex_path):
    """从 .ctex 文件名反推原始文件名(无 .import/.remap 映射时的回退)。

    Godot 导入纹理命名: <原始名>-<MD5哈希>.ctex, 如 foo.png-1a2b3c.ctex。
    这里去掉末尾 -<hash>, 并把扩展名统一成 .png, 得到 foo.png。
    """
    base = os.path.basename(ctex_path)
    if base.lower().endswith(".ctex"):
        base = base[:-5]
    m = re.match(r"^(.*?)(?:-[0-9a-fA-F]{16,64})?$", base)
    name = m.group(1) if m else base
    # name 可能已带原扩展名(如 5.png), 统一去掉后补 .png, 得到干净的 5.png
    stem, _ = os.path.splitext(name)
    return (stem or name) + ".png"


def maybe_png(raw, fmt, out_path):
    """尝试用 Pillow 把 WebP/PNG 转成 PNG; 失败则原样保存。"""
    base, _ = os.path.splitext(out_path)
    png_path = base + ".png"
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        img.save(png_path, "PNG")
        return png_path, "PNG"
    except Exception:
        # 回退: 保存原始格式
        ext = ".webp" if fmt == "WEBP" else ".png"
        fb_path = base + ext
        with open(fb_path, "wb") as o:
            o.write(raw)
        return fb_path, ext[1:].upper()


# ---------- Godot 4 二进制资源 (RSRC / RSCC) 解析 ----------
# 打包后的音频为 .sample (AudioStreamWAV 资源, 内含裸 PCM),
# 字体为 .fontdata (FontFile 资源, 用 RSCC+zstd 压缩保存)。
# 二者都不是能直接播放/安装的文件, 需按下述格式还原成真 .wav / .ttf。
# 参考 Godot 源码 resource_format_binary.cpp / file_access_compressed.cpp。

_V_NIL, _V_BOOL, _V_INT, _V_FLOAT, _V_STRING = 1, 2, 3, 4, 5
_V_VEC2, _V_VEC2I, _V_RECT2, _V_RECT2I = 10, 45, 11, 46
_V_VEC3, _V_VEC3I, _V_VEC4, _V_VEC4I = 12, 47, 50, 51
_V_PLANE, _V_QUAT, _V_AABB, _V_BASIS = 13, 14, 15, 16
_V_XFORM3, _V_XFORM2, _V_COLOR, _V_NODEPATH, _V_RID = 17, 18, 20, 22, 23
_V_OBJECT, _V_DICT, _V_ARRAY, _V_PBA = 24, 26, 30, 31
_V_PI32, _V_PF32, _V_PSTR, _V_PV3, _V_PCOL, _V_PV2 = 32, 33, 34, 35, 36, 37
_V_INT64, _V_DBL, _V_SNAME, _V_PI64, _V_PF64, _V_PROJ = 40, 41, 44, 48, 49, 52

# 打包后可转换的媒体扩展名 -> 转换后可能的后缀
MEDIA_EXT = {".sample": ("wav",), ".fontdata": ("ttf", "otf"),
             ".oggvorbisstr": ("ogg",), ".mp3str": ("mp3",)}


def _rd_ustr(b, o):
    """读 Godot unicode 字符串 (u32 长度含 NUL + 字节)。"""
    n = struct.unpack_from("<I", b, o)[0]
    return b[o + 4:o + 4 + max(0, n - 1)].decode("utf-8", "replace"), o + 4 + n


def _scan_string_table(b, start):
    """定位字符串表。header 保留区字节数随版本而变, 故采用扫描。"""
    N = len(b)
    for st in range(start, min(start + 4096, N - 8)):
        cnt = struct.unpack_from("<I", b, st)[0]
        if not (1 <= cnt <= 65535):
            continue
        q = st + 4
        names = []
        ok = True
        for _ in range(cnt):
            if q + 4 > N:
                ok = False
                break
            ln = struct.unpack_from("<I", b, q)[0]
            if not (1 <= ln <= 4096) or q + 4 + ln > N:
                ok = False
                break
            raw = b[q + 4:q + 4 + ln]
            if any(c != 0 and (c < 32 or c > 126) for c in raw):
                ok = False
                break
            names.append(raw.rstrip(b"\x00").decode("utf-8", "replace"))
            q = q + 4 + ln
        if ok and len(names) == cnt:
            return names, q
    raise ValueError("未找到字符串表 (非预期格式)")


def _rd_variant(b, o, rdbl):
    """读一个 Variant, 返回 (值, 新偏移)。仅需跳过+取用常见类型。"""
    t = struct.unpack_from("<I", b, o)[0]
    o += 4
    if t == _V_NIL:
        return None, o
    if t == _V_BOOL:
        return bool(struct.unpack_from("<I", b, o)[0]), o + 4
    if t == _V_INT:
        return struct.unpack_from("<i", b, o)[0], o + 4
    if t == _V_INT64:
        return struct.unpack_from("<q", b, o)[0], o + 8
    if t == _V_FLOAT:
        if rdbl:
            return struct.unpack_from("<d", b, o)[0], o + 8
        return struct.unpack_from("<f", b, o)[0], o + 4
    if t == _V_DBL:
        return struct.unpack_from("<d", b, o)[0], o + 8
    if t in (_V_STRING, _V_SNAME):
        return _rd_ustr(b, o)
    if t == _V_PBA:
        n = struct.unpack_from("<I", b, o)[0]
        return b[o + 4:o + 4 + n], o + 4 + n
    _packed = {_V_PI32: 4, _V_PF32: 4, _V_PI64: 8, _V_PF64: 8, _V_PV2: 8,
               _V_PV3: 12, _V_PCOL: 16}
    if t in _packed:
        n = struct.unpack_from("<I", b, o)[0]
        return ("packed", t, n), o + 4 + _packed[t] * n
    if t == _V_PSTR:
        n = struct.unpack_from("<I", b, o)[0]
        o += 4
        for _ in range(n):
            _, o = _rd_ustr(b, o)
        return ("packed_str", n), o
    if t == _V_OBJECT:
        sub = struct.unpack_from("<I", b, o)[0]
        o += 4
        if sub in (1, 2):
            return ("object", sub, struct.unpack_from("<I", b, o)[0]), o + 4
        return ("object", sub), o
    if t in (_V_ARRAY, _V_DICT):
        n = struct.unpack_from("<I", b, o)[0]
        o += 4
        for _ in range(n if t == _V_ARRAY else n * 2):
            _, o = _rd_variant(b, o, rdbl)
        return ("array" if t == _V_ARRAY else "dict", n), o
    if t == _V_NODEPATH:
        nc = struct.unpack_from("<I", b, o)[0]
        sc = struct.unpack_from("<I", b, o + 4)[0]
        o += 12
        for _ in range(nc + sc):
            idx = struct.unpack_from("<I", b, o)[0]
            o += 4
            if idx & 0x80000000:
                o += idx & 0x7FFFFFFF
        return ("nodepath",), o
    if t == _V_RID:
        return ("rid",), o + 8
    _fixed = {_V_VEC2: 8, _V_VEC2I: 8, _V_RECT2: 16, _V_RECT2I: 16,
              _V_VEC3: 12, _V_VEC3I: 12, _V_VEC4: 16, _V_VEC4I: 16,
              _V_PLANE: 16, _V_QUAT: 16, _V_AABB: 24, _V_BASIS: 36,
              _V_XFORM3: 48, _V_XFORM2: 24, _V_PROJ: 64}
    if t in _fixed:
        n = _fixed[t]
        if rdbl:
            n = n // 4 * 8
        return ("fixed", t), o + n
    raise ValueError("未支持的 Variant 类型: %d" % t)


def parse_rsrc(buf):
    """解析 Godot 4 二进制资源 (RSRC), 返回 {type, resources:[{type, props}]}。"""
    if buf[:4] != b"RSRC":
        raise ValueError("不是 RSRC 资源 (magic=%r)" % buf[:4])
    p = 4
    vmaj, vmin, vfmt = (struct.unpack_from("<I", buf, p + 8)[0],
                        struct.unpack_from("<I", buf, p + 12)[0],
                        struct.unpack_from("<I", buf, p + 16)[0])
    p += 20
    type_str, p = _rd_ustr(buf, p)
    p += 8                       # importmd_ofs
    flags = struct.unpack_from("<I", buf, p)[0]
    p += 4
    using_uids = bool(flags & 2)
    real_double = bool(flags & 4)
    p += 8                       # uid (无 uid 时为保留 0)
    names, p = _scan_string_table(buf, p)
    ext_n = struct.unpack_from("<I", buf, p)[0]
    p += 4
    for _ in range(ext_n):
        p += 4                   # 类型在字符串表中的索引
        _, p = _rd_ustr(buf, p)  # 路径
        if using_uids:
            p += 8               # uid
    int_n = struct.unpack_from("<I", buf, p)[0]
    p += 4
    for _ in range(int_n):
        _, p = _rd_ustr(buf, p)
        p += 8                   # offset
    resources = []
    for _ in range(int_n):
        rt, p = _rd_ustr(buf, p)
        pc = struct.unpack_from("<I", buf, p)[0]
        p += 4
        props = {}
        for _ in range(pc):
            ni = struct.unpack_from("<I", buf, p)[0]
            p += 4
            val, p = _rd_variant(buf, p, real_double)
            props[names[ni] if 0 <= ni < len(names) else ("#%d" % ni)] = val
        resources.append({"type": rt, "props": props})
    return {"type": type_str, "ver": (vmaj, vmin, vfmt),
            "flags": flags, "resources": resources}


def decompress_rscc(buf):
    """解压 Godot 压缩资源容器 (RSCC, FileAccessCompressed + zstd 块)。

    注意: 压缩保存的资源内部会省略开头的 'RSRC' 魔术字。
    """
    if buf[:4] != b"RSCC":
        raise ValueError("不是 RSCC 容器")
    cmode = struct.unpack_from("<I", buf, 4)[0]
    block_size = struct.unpack_from("<I", buf, 8)[0]
    total = struct.unpack_from("<I", buf, 12)[0]
    if cmode != 2:
        raise ValueError("RSCC 压缩模式 %d 非 ZSTD" % cmode)
    bc = (total // block_size) + 1
    sizes = [struct.unpack_from("<I", buf, 16 + 4 * i)[0] for i in range(bc)]
    pos = 16 + 4 * bc
    try:
        import zstandard as zstd
    except ImportError:
        raise RuntimeError("解压 .fontdata 需要 zstandard 模块 (pip install zstandard)")
    dctx = zstd.ZstdDecompressor()
    out = bytearray()
    for s in sizes:
        out += dctx.decompress(buf[pos:pos + s], max_output_size=block_size)
        pos += s
    return bytes(out[:total]) if total else bytes(out)


_FONT_SIGS = ((b"\x00\x01\x00\x00", "ttf"), (b"OTTO", "otf"),
              (b"true", "ttf"), (b"typ1", "ttf"),
              (b"ttcf", "ttc"), (b"wOFF", "woff"), (b"wOF2", "woff2"))


def _font_sig(d):
    for sig, ext in _FONT_SIGS:
        if d[:len(sig)] == sig:
            return ext
    return None


def _find_pba(buf, sig):
    """在 RSRC 中查找内容以 sig 开头的 PackedByteArray, 返回其字节。"""
    i = 0
    tag = struct.pack("<I", _V_PBA)
    while True:
        j = buf.find(tag, i)
        if j < 0:
            return None
        if j + 8 <= len(buf):
            ln = struct.unpack_from("<I", buf, j + 4)[0]
            if 0 < ln <= len(buf) - (j + 8) and buf[j + 8:j + 8 + len(sig)] == sig:
                return bytes(buf[j + 8:j + 8 + ln])
        i = j + 1


def build_wav(pcm, fmt, mix_rate, stereo):
    """把裸 PCM 封装成标准 RIFF/WAVE。fmt: 0=8bit 1=16bit (Godot 已交错)。"""
    if fmt == 1:
        bits = 16
    elif fmt == 0:
        bits = 8
    else:
        return None            # 2=IMA ADPCM, 3=QOA: 非 PCM, 不处理
    ch = 2 if stereo else 1
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
           + b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, mix_rate,
                                   mix_rate * ch * bits // 8, ch * bits // 8, bits)
           + b"data" + struct.pack("<I", len(pcm)))
    return hdr + pcm


def convert_sample(buf):
    """.sample (AudioStreamWAV) -> wav 字节。失败返回 None。"""
    r = parse_rsrc(buf)
    if r["type"] != "AudioStreamWAV" or not r["resources"]:
        return None
    props = r["resources"][0]["props"]
    data = props.get("data")
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    return build_wav(bytes(data), int(props.get("format", 0)),
                     int(props.get("mix_rate", 44100) or 44100),
                     bool(props.get("stereo", False)))


def convert_fontdata(buf):
    """.fontdata (FontFile, RSCC/zstd) -> (字体字节, 后缀)。失败返回 None。"""
    inner = decompress_rscc(buf)
    e = _font_sig(inner)
    if e:
        return inner, e
    body = inner if inner[:4] == b"RSRC" else (b"RSRC" + inner)
    try:
        r = parse_rsrc(body)
    except Exception:
        r = None
    if r:
        for res in r["resources"]:
            d = res["props"].get("data")
            if isinstance(d, (bytes, bytearray)) and len(d) > 64:
                e = _font_sig(d)
                if e:
                    return bytes(d), e
                if bytes(d[:4]) not in (b"\x00\x00\x00\x00", b"RSRC"):
                    return bytes(d), "ttf"
    for sig, ext in _FONT_SIGS:
        x = _find_pba(body, sig)
        if x:
            return x, ext
    return None


def extract_ogg(buf):
    """.oggvorbisstr -> 原始 ogg 流字节。"""
    if buf[:4] == b"OggS":
        return buf
    return _find_pba(buf, b"OggS")


def extract_mp3(buf):
    """.mp3str -> 原始 mp3 字节。"""
    for sig in (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        if buf[:len(sig)] == sig:
            return buf
        d = _find_pba(buf, sig)
        if d:
            return d
    return None


def convert_media_asset(data, ext):
    """把打包后的媒体资源还原成可用格式, 返回 (字节, 后缀) 或 None。"""
    try:
        if ext == ".sample":
            w = convert_sample(data)
            return (w, "wav") if w else None
        if ext == ".fontdata":
            return convert_fontdata(data)
        if ext == ".oggvorbisstr":
            o = extract_ogg(data)
            return (o, "ogg") if o else None
        if ext == ".mp3str":
            m = extract_mp3(data)
            return (m, "mp3") if m else None
    except Exception:
        return None
    return None


def _media_basename(raw_path, out_ext):
    """无 .import 映射时的回退名称: 从 foo.wav-<hash>.sample 反推 foo.wav。"""
    base = os.path.splitext(os.path.basename(raw_path))[0]   # 去 .sample
    base = re.sub(r"-[0-9a-fA-F]{16,64}$", "", base)         # 去 -<hash>
    if not os.path.splitext(base)[1]:
        base += "." + out_ext
    return base


# ---------- 筛选 ----------
def _split_list(s):
    """把 'a, b c' 这类字符串拆成小写列表 (去前导 /)。空返回 None。"""
    if not s:
        return None
    return [x.strip().lower().lstrip("/") for x in re.split(r"[,\s]+", s) if x.strip()]


def passes_filter(filt_path, ext_keys, include, exclude, exts):
    """filt_path: 用于前缀/后缀匹配的(归位后)路径; ext_keys: 可接受的扩展名(源+转换后)。
    include/exclude 规则: 含'/'当路径片段, 以'.'开头且无'/'当扩展名(后缀), 其余当路径前缀。
    """
    fp = filt_path.lower()

    def match(p):
        p = p.lower()
        if p.startswith("."):
            return (fp.endswith(p) or fp == p
                    or fp.startswith(p + "/") or ("/" + p + "/") in fp)
        return fp == p or fp.startswith(p + "/") or ("/" + p + "/") in fp

    if exclude:
        for p in exclude:
            if match(p):
                return False
    if include:
        if not any(match(p) for p in include):
            return False
    if exts:
        if isinstance(ext_keys, str):
            ext_keys = [ext_keys]
        if not any(k.lower().lstrip(".") in exts for k in ext_keys):
            return False
    return True


# ---------- 主解包 ----------
def unpack(pck_path, output_dir, convert=True, organize=True, convert_media=True,
           keep_webp=False, keep_raw=False, list_only=False, quiet=False,
           skip_meta=False, include=None, exclude=None, exts=None,
           log_cb=None, progress_cb=None, cancel_cb=None):
    """解包主函数。
    log_cb(msg, is_err)   : 日志回调 (不传则打印到控制台)
    progress_cb(cur,total): 进度回调 (cur=已处理条目数, total=总条目数)
    cancel_cb()->bool     : 返回 True 时中止解包
    """
    def _log(msg, is_err=False):
        if log_cb:
            log_cb(msg, is_err)
        else:
            (eprint if is_err else print)(msg)

    def _prog(cur, total):
        if progress_cb:
            progress_cb(cur, total)

    fsize = os.path.getsize(pck_path)
    cancelled = False
    with open(pck_path, "rb") as f:
        hdr = parse_header(f)
        ver = hdr["version"]
        godot = "%d.%d.%d" % (hdr["major"], hdr["minor"], hdr["patch"])
        if not quiet:
            _log("[PCK] Godot %s  (PCK format v%d)" % (godot, ver))
            _log("[PCK] 大小 %d 字节, FileOffsetBase=0x%X, 目录偏移=0x%X"
                  % (fsize, hdr["fob"], directory_offset(hdr)))

        entries = parse_directory(f, hdr, fsize)
        if not quiet:
            _log("[PCK] 共 %d 个文件" % len(entries))
        total = len(entries)

        # 构建 .import 映射 (供归位/预览使用, 只读不写)
        imp_map = (build_import_map(f, entries, fsize)
                   if organize and (convert or convert_media) else {})

        def _out_exts(ext):
            """可接受的扩展名: 源扩展名 + 转换后可能的后缀 (便于 --ext wav/ttf/png)。"""
            keys = [ext.lstrip(".")]
            if convert and ext in (".ctex", ".stex"):
                keys.append("png")
            if convert_media and ext in MEDIA_EXT:
                keys.extend(MEDIA_EXT[ext])
            return keys

        def _filt_key(rel, ext):
            """定位后的路径 (可转换资产用原始开发路径, 便于 --include Sounds 等)。"""
            conv = ((convert and ext in (".ctex", ".stex"))
                    or (convert_media and ext in MEDIA_EXT))
            if organize and conv:
                bn = os.path.basename(rel)
                if bn in imp_map:
                    return imp_map[bn].lstrip("/")
            return rel

        if list_only:
            for e in entries:
                rel = e["path"]
                if rel.startswith(RES_PREFIX):
                    rel = rel[len(RES_PREFIX):]
                rel = rel.replace("\x00", "")
                ext = os.path.splitext(rel)[1].lower()
                if skip_meta and ext in (".import", ".remap"):
                    continue
                # 纹理/音频/字体以归位后的原始路径展示 (与提取一致)
                filt_key = _filt_key(rel, ext)
                if not passes_filter(filt_key, _out_exts(ext), include, exclude, exts):
                    continue
                _log("  %8d  %s" % (e["size"], filt_key))
            return

        # 重新打开以便按需 seek 读取 (上面 build_import_map 已 seek)
        extracted = skipped = errors = enc = skipped_filter = skipped_meta = 0
        for idx, ent in enumerate(entries):
            if cancel_cb and cancel_cb():
                cancelled = True
                _log("[!] 用户取消, 已处理 %d/%d" % (idx, total))
                break

            _prog(idx + 1, total)

            # 路径清洗
            rel = ent["path"]
            if rel.startswith(RES_PREFIX):
                rel = rel[len(RES_PREFIX):]
            rel = rel.replace("\x00", "")
            if not rel:
                rel = "file_%d" % idx

            out_path = os.path.join(output_dir, rel)
            ext = os.path.splitext(rel)[1].lower()

            # 可选跳过导入元数据 (.import/.remap) —— 纯文本, 改后缀也打不开, 非可用资源
            if skip_meta and ext in (".import", ".remap"):
                skipped_meta += 1
                continue

            # 筛选: 以最终输出路径为准 (纹理/音频/字体归位后落在原始目录)
            filt_key = _filt_key(rel, ext)
            if not passes_filter(filt_key, _out_exts(ext), include, exclude, exts):
                skipped_filter += 1
                continue

            # 越界检查
            if ent["abs_offset"] + ent["size"] > fsize:
                _log("[!] 越界跳过: %s (off=0x%X size=%d)"
                     % (ent["path"], ent["abs_offset"], ent["size"]), is_err=True)
                errors += 1
                continue

            # 加密文件
            if ent["flags"] & PCK_FILE_ENCRYPTED:
                enc += 1
                if not quiet:
                    _log("[*] 加密文件跳过: %s" % ent["path"], is_err=True)
                continue

            # 读取数据
            f.seek(ent["abs_offset"])
            data = f.read(ent["size"])

            # 纹理转换
            is_tex = ext in (".ctex", ".stex")
            if convert and is_tex:
                img, fmt = convert_texture(data, ext)
                if img is not None:
                    wrote = False
                    if organize:
                        base_name = os.path.basename(ent["path"])
                        if base_name in imp_map:
                            orig = imp_map[base_name]
                            orig_png = os.path.splitext(
                                os.path.join(output_dir, orig))[0] + ".png"
                            os.makedirs(os.path.dirname(orig_png), exist_ok=True)
                            maybe_png(img, fmt, orig_png)
                            wrote = True
                    if not wrote:
                        # 无映射: 用 .ctex 文件名反推原始名, 落到同目录 (.godot/imported/)
                        fb = _ctex_basename(ent["path"])
                        fb_path = os.path.join(os.path.dirname(out_path) or output_dir, fb)
                        os.makedirs(os.path.dirname(fb_path) or output_dir, exist_ok=True)
                        maybe_png(img, fmt, fb_path)
                    extracted += 1
                    if keep_webp:
                        wp = os.path.splitext(
                            (orig_png if wrote else out_path))[0] + ".webp"
                        with open(wp, "wb") as wf:
                            wf.write(img)
                    if keep_raw:
                        # 额外保留原始 .ctex/.stex 以备工程重建
                        os.makedirs(os.path.dirname(out_path) or output_dir, exist_ok=True)
                        with open(out_path, "wb") as o:
                            o.write(data)
                        extracted += 1
                    continue

            # 音频/字体转换: .sample -> .wav, .fontdata -> .ttf/.otf
            if convert_media and ext in MEDIA_EXT:
                conv = convert_media_asset(data, ext)
                if conv is not None:
                    mbytes, mext = conv
                    tgt = None
                    bn = os.path.basename(ent["path"])
                    if organize and bn in imp_map:
                        # 归位到开发时原始路径 (如 Sounds/Bump.wav)
                        orig = imp_map[bn]
                        tgt = os.path.join(output_dir,
                                           os.path.splitext(orig)[0] + "." + mext)
                    if tgt is None:
                        # 回退: 从 foo.wav-<hash>.sample 反推原名, 落到同目录
                        tgt = os.path.join(os.path.dirname(out_path) or output_dir,
                                           _media_basename(ent["path"], mext))
                    os.makedirs(os.path.dirname(tgt) or output_dir, exist_ok=True)
                    try:
                        with open(tgt, "wb") as o:
                            o.write(mbytes)
                        extracted += 1
                    except Exception as e:
                        _log("[!] 写入失败 %s: %s" % (ent["path"], e), is_err=True)
                        errors += 1
                        continue
                    if keep_raw:
                        os.makedirs(os.path.dirname(out_path) or output_dir,
                                    exist_ok=True)
                        with open(out_path, "wb") as o:
                            o.write(data)
                        extracted += 1
                    continue

            # 普通文件: 原样写出
            os.makedirs(os.path.dirname(out_path) or output_dir, exist_ok=True)
            try:
                with open(out_path, "wb") as o:
                    o.write(data)
                extracted += 1
            except Exception as e:
                _log("[!] 写入失败 %s: %s" % (ent["path"], e), is_err=True)
                errors += 1

        _log("\n[完成] 提取 %d, 跳过(加密) %d, 跳过元数据 %d, 筛选排除 %d, 错误 %d%s"
             % (extracted, enc, skipped_meta, skipped_filter, errors,
                ("  (已取消)" if cancelled else "")))
        _log("[完成] 输出目录: %s" % output_dir)


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(
        description="Godot PCK 解包工具 (支持 Godot 3/4, 自动识别版本)")
    ap.add_argument("pck", help="PCK 文件路径")
    ap.add_argument("-o", "--output", help="输出目录 (默认: <pck名>_unpacked)")
    ap.add_argument("--no-convert", action="store_true",
                    help="不把纹理 (.ctex/.stex) 转成 PNG/WebP")
    ap.add_argument("--no-media", action="store_true",
                    help="不把音频 (.sample) / 字体 (.fontdata) 还原成 .wav/.ttf")
    ap.add_argument("--no-organize", action="store_true",
                    help="不按 .import 映射把纹理归位到原始路径")
    ap.add_argument("--keep-webp", action="store_true",
                    help="转 PNG 时额外保留 .webp 原文件")
    ap.add_argument("--keep-raw", action="store_true",
                    help="额外保留原始 .ctex/.stex (用于 Godot 工程重建)")
    ap.add_argument("--no-meta", action="store_true",
                    help="不导出导入元数据 (.import/.remap, 纯文本约1KB, 改后缀也打不开)")
    ap.add_argument("--include", help="只导出这些路径前缀 (逗号/空格分隔, 如 Sprites,Sounds,UI)")
    ap.add_argument("--exclude", help="排除这些路径前缀 (逗号/空格分隔)")
    ap.add_argument("--ext", help="只导出这些扩展名 (源或转换后, 逗号/空格分隔, 如 png,ctex,wav,ttf)")
    ap.add_argument("--list", action="store_true",
                    help="仅列出包内文件, 不解包")
    ap.add_argument("-q", "--quiet", action="store_true", help="减少输出")
    args = ap.parse_args()

    if not os.path.isfile(args.pck):
        eprint("文件不存在: %s" % args.pck)
        sys.exit(1)

    out = args.output or (os.path.splitext(args.pck)[0] + "_unpacked")

    try:
        unpack(
            args.pck,
            out,
            convert=not args.no_convert,
            organize=not args.no_organize,
            convert_media=not args.no_media,
            keep_webp=args.keep_webp,
            keep_raw=args.keep_raw,
            list_only=args.list,
            skip_meta=args.no_meta,
            quiet=args.quiet,
            include=_split_list(args.include),
            exclude=_split_list(args.exclude),
            exts=_split_list(args.ext),
        )
    except Exception as e:
        eprint("[错误] %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
