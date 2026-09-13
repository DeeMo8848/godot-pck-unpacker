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
  - 利用 .import 映射把纹理归位到开发时的原始路径 (干净文件名)
  - 加密文件自动跳过并告警

依赖:
  - 仅需 Python 3 (标准库)
  - 可选: Pillow  (用于把 WebP 转成 PNG; 没有则直接保存 .webp)

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
    """扫描 .import 条目, 建立 ctex文件名 -> 原始资源路径 的映射。"""
    imp_map = {}
    pat = re.compile(r'path="res://\.godot/imported/([^"]+\.ctex)"')
    for ent in entries:
        if not ent["path"].endswith(".import"):
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
            ctex_name = m.group(1)
            orig = ent["path"][: -len(".import")]
            imp_map[ctex_name] = orig
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


# ---------- 筛选 ----------
def _split_list(s):
    """把 'a, b c' 这类字符串拆成小写列表 (去前导 /)。空返回 None。"""
    if not s:
        return None
    return [x.strip().lower().lstrip("/") for x in re.split(r"[,\s]+", s) if x.strip()]


def passes_filter(filt_path, src_ext, include, exclude, exts):
    """filt_path: 用于前缀/后缀匹配的(归位后)路径; src_ext: 源文件扩展名(用于 --ext)。
    include/exclude 规则: 含'/'当路径片段, 以'.'开头且无'/'当扩展名(后缀), 其余当路径前缀。
    """
    fp = filt_path.lower()

    def match(p):
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
        if src_ext.lower().lstrip(".") not in exts:
            return False
    return True


# ---------- 主解包 ----------
def unpack(pck_path, output_dir, convert=True, organize=True, keep_webp=False,
           keep_raw=False, list_only=False, quiet=False,
           include=None, exclude=None, exts=None,
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
        imp_map = build_import_map(f, entries, fsize) if (convert and organize) else {}

        if list_only:
            for e in entries:
                rel = e["path"]
                if rel.startswith(RES_PREFIX):
                    rel = rel[len(RES_PREFIX):]
                rel = rel.replace("\x00", "")
                ext = os.path.splitext(rel)[1].lower()
                # 纹理以归位后的原始路径展示 (与提取一致)
                filt_key = rel
                if ext in (".ctex", ".stex"):
                    bn = os.path.basename(rel)
                    if bn in imp_map:
                        filt_key = imp_map[bn].lstrip("/")
                if not passes_filter(filt_key, ext, include, exclude, exts):
                    continue
                _log("  %8d  %s" % (e["size"], filt_key))
            return

        # 重新打开以便按需 seek 读取 (上面 build_import_map 已 seek)
        extracted = skipped = errors = enc = skipped_filter = 0
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

            # 筛选: 以最终输出路径为准 (纹理归位后落在原始目录)
            filt_key = rel
            if convert and organize and ext in (".ctex", ".stex"):
                bn = os.path.basename(ent["path"])
                if bn in imp_map:
                    filt_key = imp_map[bn].lstrip("/")
            if not passes_filter(filt_key, ext, include, exclude, exts):
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
                        # 无映射: 写到 res:// 对应位置
                        os.makedirs(os.path.dirname(out_path) or output_dir, exist_ok=True)
                        maybe_png(img, fmt, out_path)
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

            # 普通文件: 原样写出
            os.makedirs(os.path.dirname(out_path) or output_dir, exist_ok=True)
            try:
                with open(out_path, "wb") as o:
                    o.write(data)
                extracted += 1
            except Exception as e:
                _log("[!] 写入失败 %s: %s" % (ent["path"], e), is_err=True)
                errors += 1

        _log("\n[完成] 提取 %d, 跳过(加密) %d, 筛选排除 %d, 错误 %d%s"
             % (extracted, enc, skipped_filter, errors,
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
    ap.add_argument("--no-organize", action="store_true",
                    help="不按 .import 映射把纹理归位到原始路径")
    ap.add_argument("--keep-webp", action="store_true",
                    help="转 PNG 时额外保留 .webp 原文件")
    ap.add_argument("--keep-raw", action="store_true",
                    help="额外保留原始 .ctex/.stex (用于 Godot 工程重建)")
    ap.add_argument("--include", help="只导出这些路径前缀 (逗号/空格分隔, 如 Sprites,Sounds,UI)")
    ap.add_argument("--exclude", help="排除这些路径前缀 (逗号/空格分隔)")
    ap.add_argument("--ext", help="只导出这些源扩展名 (逗号/空格分隔, 如 ctex,scn,gd)")
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
            keep_webp=args.keep_webp,
            keep_raw=args.keep_raw,
            list_only=args.list,
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
