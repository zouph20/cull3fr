#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cull3fr：超轻量 Hasselblad 选片（1 到 5 星 + 红黄绿）

· 直接读相机内嵌的预览图（快），评分和颜色记在 3FR 上；只有 HEIC、没有 3FR 的照片记在 HEIC 上
· 打开时读取已有评分；HEIF 和 RAW 评分不一致时按 RAW，并标「不一致」
· 选片过程只写工具自己的记录 .cull3fr.json，不碰照片
· 最后点「全部写入」，一次写进 3FR 旁边的 Phocus .phos
  （颜色 = 顶层 Approval：绿 0 / 黄 1 / 红 2；星级 = 顶层 XMPData 里的 xap:Rating。
    按 Phocus 4.x 实际写出的 .phos 核对过；没有 .phos 的 3FR 会新建一个和 Phocus 同格式的）
· 每次写入前自动备份 .phos，可撤销

用法:
  python3 cull3fr.py                 # 弹窗选文件夹
  python3 cull3fr.py /path/to/shoot  # 直接打开
  python3 cull3fr.py /path --undo    # 撤销最近一次写入 / 移动（网页里也有撤销按钮）
  python3 cull3fr.py --install-app   # 生成「Cull3fr」App，以后双击打开
  python3 cull3fr.py /path --inspect # 只打印每张图读到的评分，不开界面

只用 Python 标准库 + macOS 自带 sips。

项目名 cull3fr，MIT License（见 LICENSE）。
与 Hasselblad 无关；Phocus、X2D 是 Hasselblad 的商标。
"""
import argparse
import hashlib
import hmac
import io
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.5.1"
RAW_EXTS = (".3fr", ".fff")
COMP_EXTS = (".heif", ".heic", ".hif", ".jpg", ".jpeg")  # 越靠前越优先用来预览
APPROVAL = {"green": 0, "yellow": 1, "red": 2}           # Phocus .phos Approval
APPROVAL_INV = {0: "green", 2: "red"}                     # 1 是 Phocus 默认值，和「未标」分不开
OUT_DIRS = {"green": "_green", "yellow": "_yellow", "red": "_red"}
STATE_FILE = ".cull3fr.json"
LOG_FILE = ".cull3fr_log.json"
BACKUP_DIR = ".cull3fr_backup"
# 0.5 以前叫 cull3fr，用的是这些文件名：第一次打开时自动接着用里面的标记和撤销记录
LEGACY_STATE_FILE = ".hb_cull.json"
LEGACY_LOG_FILE = ".hb_cull_log.json"
SIPS = shutil.which("sips") if sys.platform == "darwin" else None


def clamp(r):
    return max(0, min(5, int(r)))


# ===================================================================== 读评分

_XMP_RATING = [
    re.compile(r"<(?:xmp|xap):Rating>\s*(-?\d+)\s*</(?:xmp|xap):Rating>"),
    re.compile(r"(?:xmp|xap):Rating\s*=\s*[\"'](-?\d+)[\"']"),
]


def xmp_rating(text):
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", "ignore")
    for rx in _XMP_RATING:
        m = rx.search(text)
        if m:
            return clamp(m.group(1))
    return None


def tiff_rating(f, base=0):
    """TIFF（3FR / FFF / Exif 块）IFD0 里的 XMP Rating，其次 EXIF Rating(0x4746)。"""
    try:
        f.seek(base)
        h = f.read(8)
        if h[:2] == b"II":
            e = "<"
        elif h[:2] == b"MM":
            e = ">"
        else:
            return None
        ifd = struct.unpack(e + "I", h[4:8])[0]
        f.seek(base + ifd)
        n = struct.unpack(e + "H", f.read(2))[0]
        if not 0 < n < 2000:
            return None
        raw = f.read(12 * n)
        exif_r, xmp = None, None
        for i in range(n):
            tag, typ, cnt = struct.unpack(e + "HHI", raw[i * 12:i * 12 + 8])
            val = raw[i * 12 + 8:i * 12 + 12]
            if tag == 0x4746:
                if typ == 3:
                    exif_r = struct.unpack(e + "H", val[:2])[0]
                elif typ == 4:
                    exif_r = struct.unpack(e + "I", val)[0]
            elif tag == 0x02BC and 0 < cnt < 8000000:
                if cnt <= 4:
                    xmp = val[:cnt]
                else:
                    f.seek(base + struct.unpack(e + "I", val)[0])
                    xmp = f.read(cnt)
        r = xmp_rating(xmp)
        if r is None and exif_r is not None:
            r = clamp(exif_r)
        return r
    except Exception:
        return None


def scan_rating(path, nbytes=256 << 10):
    """兜底：在文件开头找 XMP Rating 文本。"""
    try:
        with open(path, "rb") as f:
            return xmp_rating(f.read(nbytes))
    except OSError:
        return None


def jpeg_rating(path):
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            r_exif = r_xmp = None
            while True:
                b = f.read(4)
                if len(b) < 4 or b[0] != 0xFF or b[1] in (0xDA, 0xD9):
                    break
                ln = struct.unpack(">H", b[2:4])[0]
                start = f.tell()
                if b[1] == 0xE1:
                    d = f.read(ln - 2)
                    if d.startswith(b"Exif\x00\x00"):
                        r_exif = tiff_rating(io.BytesIO(d[6:]))
                    elif d.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                        r_xmp = xmp_rating(d[29:])
                f.seek(start + ln - 2)
            return r_xmp if r_xmp is not None else r_exif
    except Exception:
        return None


def _boxes(b, start, end):
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", b[pos:pos + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", b[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break
        yield typ.decode("latin1"), pos + hdr, pos + size
        pos += size


def _parse_iinf(b, s, e):
    ver = b[s]
    p = s + 4 + (2 if ver == 0 else 4)
    out = {}
    for typ, cs, ce in _boxes(b, p, e):
        if typ != "infe" or b[cs] < 2:
            continue
        v, q = b[cs], cs + 4
        if v == 2:
            iid = struct.unpack(">H", b[q:q + 2])[0]
            q += 2
        else:
            iid = struct.unpack(">I", b[q:q + 4])[0]
            q += 4
        q += 2
        itype = b[q:q + 4].decode("latin1")
        q += 4
        z = b.find(b"\x00", q, ce)
        q = (z + 1) if z >= 0 else ce
        ctype = None
        if itype == "mime" and q < ce:
            z = b.find(b"\x00", q, ce)
            ctype = b[q:z if z >= 0 else ce].decode("latin1")
        out[iid] = (itype, ctype)
    return out


def _parse_iloc(b, s, e):
    ver = b[s]
    st = {"p": s + 4}

    def rd(n):
        if n == 0:
            return 0
        if st["p"] + n > e:
            raise ValueError("iloc 越界")
        v = int.from_bytes(b[st["p"]:st["p"] + n], "big")
        st["p"] += n
        return v

    x = rd(1)
    osz, lsz = x >> 4, x & 15
    x = rd(1)
    bsz, isz = x >> 4, ((x & 15) if ver in (1, 2) else 0)
    if osz not in (0, 4, 8) or lsz not in (0, 4, 8) or bsz not in (0, 4, 8) or isz not in (0, 4, 8):
        raise ValueError("iloc 字段长度不合法")
    count = rd(2 if ver < 2 else 4)
    if count > (e - st["p"]) // 4:  # 每项至少 4 字节，防止伪造的超大计数卡死
        raise ValueError("iloc 项数不合理")
    out = {}
    for _ in range(count):
        iid = rd(2 if ver < 2 else 4)
        cm = (rd(2) & 15) if ver in (1, 2) else 0
        rd(2)
        base = rd(bsz)
        n = rd(2)
        if osz + lsz == 0 or n > 4096 or n * (osz + lsz + isz) > e - st["p"]:
            raise ValueError("extent 数不合理")
        ext = []
        for _ in range(n):
            if isz:
                rd(isz)
            off = rd(osz)
            ext.append((base + off, rd(lsz)))
        out[iid] = (cm, ext)
    return out


def heif_rating(path):
    """HEIF：读 meta 里的 Exif / XMP item。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            pos, meta = 0, None
            while pos + 8 <= fsize:
                f.seek(pos)
                h = f.read(16)
                size, typ = struct.unpack(">I4s", h[:8])
                hdr = 8
                if size == 1:
                    size = struct.unpack(">Q", h[8:16])[0]
                    hdr = 16
                elif size == 0:
                    size = fsize - pos
                if size < hdr:
                    break
                if typ == b"meta":
                    if size > (2 << 20):
                        return None
                    f.seek(pos + hdr)
                    meta = f.read(size - hdr)
                    break
                pos += size
            if meta is None:
                return None
            items, locs, idat = {}, {}, None
            for typ, s, e in _boxes(meta, 4, len(meta)):
                if typ == "iinf":
                    items = _parse_iinf(meta, s, e)
                elif typ == "iloc":
                    locs = _parse_iloc(meta, s, e)
                elif typ == "idat":
                    idat = (s, e)
            r_exif = r_xmp = None
            for iid, (itype, ctype) in items.items():
                is_xmp = itype == "mime" and "rdf+xml" in (ctype or "")
                if iid not in locs or not (itype == "Exif" or is_xmp):
                    continue
                cm, ext = locs[iid]
                parts = []
                for off, ln in ext:
                    if ln <= 0 or ln > (16 << 20):
                        parts = None
                        break
                    if cm == 0:
                        f.seek(off)
                        parts.append(f.read(ln))
                    elif cm == 1 and idat:
                        parts.append(meta[idat[0] + off:idat[0] + off + ln])
                    else:
                        parts = None
                        break
                if not parts:
                    continue
                data = b"".join(parts)
                if itype == "Exif" and len(data) > 8:
                    off = struct.unpack(">I", data[:4])[0]
                    r_exif = tiff_rating(io.BytesIO(data[4 + off:]))
                elif is_xmp:
                    r_xmp = xmp_rating(data)
            return r_xmp if r_xmp is not None else r_exif
    except Exception:
        return None


def companion_rating(path):
    e = os.path.splitext(path)[1].lower()
    return jpeg_rating(path) if e in (".jpg", ".jpeg") else heif_rating(path)


# ===================================================================== .phos
#
# 按 Phocus 4.x 实际写出的 .phos 核对过（.phos Version 31，X2D II）：
#   · XML plist，顶层 dict
#   · 颜色 = 顶层整数 Approval（绿 0 / 黄 1 默认 / 红 2）
#   · 星级 = 顶层字符串 XMPData（一段 XMP，里面是 <xap:Rating>N</xap:Rating>）；没打过星就没有这个键
# 写入策略：只接受能被 dumps_phocus() 逐字节还原的 .phos；在字典里改 Approval / XMPData 后
# 用同一写法输出，所以除这两个值以外的字节保证不变。还原不了的文件一律跳过、不碰。


class PhosUnsupported(Exception):
    pass


class RatingUnsupported(PhosUnsupported):
    pass


# Phocus 4.x 打星时写进 .phos 的顶层 XMPData（实测原样）
PHOCUS_XMP = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
              '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Public XMP Toolkit Core 3.5">\n'
              '   <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
              '      <rdf:Description rdf:about=""\n'
              '            xmlns:xap="http://ns.adobe.com/xap/1.0/">\n'
              '         <xap:Rating>%d</xap:Rating>\n'
              '      </rdf:Description>\n'
              '   </rdf:RDF>\n'
              '</x:xmpmeta>\n\n'
              '<?xpacket end="w"?>')
_XAP_RATING = re.compile(r"(<xap:Rating>)(-?\d+)(</xap:Rating>)")


def _approval_of(pl):
    ap = pl.get("Approval")
    return ap if type(ap) is int else None


def _rating_of(pl):
    x = pl.get("XMPData")
    if isinstance(x, bytes):
        x = x.decode("utf-8", "ignore")
    return xmp_rating(x) if isinstance(x, str) else None


def phos_info_bytes(data):
    pl = plistlib.loads(data)
    if not isinstance(pl, dict):
        raise PhosUnsupported("不是 dict 结构")
    return {"approval": _approval_of(pl), "rating": _rating_of(pl)}


def phos_info(path):
    with open(path, "rb") as f:
        return phos_info_bytes(f.read())


def phos_patch(data, rating=None, approval=None):
    """改 Approval 和/或 XMPData 里的星级，返回新内容。其他字节保证不变，做不到就抛异常、不改。"""
    if data.startswith(b"bplist"):
        raise PhosUnsupported("二进制 .phos，不认识")
    old = plistlib.loads(data)
    if not isinstance(old, dict):
        raise PhosUnsupported("不是 dict 结构")
    if dumps_phocus(old) != data:
        raise PhosUnsupported("格式和 Phocus 4.x 写的不完全一样，为安全跳过")
    new = dict(old)
    if approval is not None:
        new["Approval"] = approval
    if rating is not None:
        x = old.get("XMPData")
        if x is None:
            new["XMPData"] = PHOCUS_XMP % rating  # 和 Phocus 第一次打星时一样，只加这一个键
        elif isinstance(x, str) and len(_XAP_RATING.findall(x)) == 1:
            new["XMPData"] = _XAP_RATING.sub(lambda m: m.group(1) + str(rating) + m.group(3), x)
        else:
            raise RatingUnsupported("XMPData 里的星级字段不认识")
    out = dumps_phocus(new)
    if plistlib.loads(out) != new:
        raise PhosUnsupported("校验没通过")
    info = phos_info_bytes(out)
    if approval is not None and info["approval"] != approval:
        raise PhosUnsupported("校验没通过：颜色没写进去")
    if rating is not None and info["rating"] != rating:
        raise RatingUnsupported("校验没通过：星级没写进去")
    return out


def _fmt_real(m):
    v = float(m.group(1))
    s = "0.0" if v == 0 else ("%d" % v if v.is_integer() else "%.17g" % v)
    return b"<real>" + s.encode() + b"</real>"


def dumps_phocus(d):
    """和 Phocus 自己写的 .phos 逐字节同格式（已用真实 .phos 验证往返一致）。"""
    b = plistlib.dumps(d, fmt=plistlib.FMT_XML, sort_keys=True)
    return re.sub(rb"<real>([^<]+)</real>", _fmt_real, b)


# 3FR 新 .phos 的基线设置：取自 Phocus 4.x（.phos Version 31）给 X2D II 3FR 写的 ImageSettings[0]（";;3FR"），
# 两个不同文件里这一段完全相同。文件夹里有现成 3FR .phos 时优先用那里的。
_EMBEDDED = (
    "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"
)


_EMBEDDED_HEIC = (
    "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"
)

# 新建 .phos 用的基线（ImageSettings[0]）。3FR 是 ";;3FR"，HEIC 是 ";;Initial"。
BASELINE_NAME = {"3fr": ";;3FR", "heic": ";;Initial"}


def target_kind(filename):
    e = os.path.splitext(filename)[1].lower()
    return "3fr" if e == ".3fr" else "heic" if e in (".heif", ".heic", ".hif") else None


def phos_template_from(data, kind="3fr"):
    d = plistlib.loads(data)
    s0 = (d.get("ImageSettings") or [None])[0]
    if not (isinstance(s0, dict) and s0.get("Name") == BASELINE_NAME[kind] and s0.get("Action") == 0
            and "ImageParameterCache" in d and "Version" in d):
        return None
    return {"ImageParameterCache": d["ImageParameterCache"], "ImageSettings0": s0, "Version": d["Version"]}


def embedded_template(kind="3fr"):
    import base64
    import zlib
    return plistlib.loads(zlib.decompress(base64.b64decode(_EMBEDDED if kind == "3fr" else _EMBEDDED_HEIC)))


def phos_new(tpl, approval, rating=None, kind="3fr"):
    """新建 .phos：基线设置 + 颜色 (+ 星级)。和 Phocus 只打标、不调参数时写出的结构相同。
    HEIC 的基线里有一个 Phocus 按这张图算出来的色温/色调，我们算不出来，写 0（试验性）。"""
    s0 = tpl["ImageSettings0"]
    if kind == "heic":
        s0 = dict(s0)
        s0["ImageCorrection"] = dict(s0["ImageCorrection"], ColorTemperature=0, Tint=0.0)
    d = {"Approval": approval, "CurrentIx": 0, "GUID": str(uuid.uuid4()).upper(),
         "ImageParameterCache": tpl["ImageParameterCache"], "ImageSettings": [s0],
         "Version": tpl["Version"]}
    if rating is not None:
        d["XMPData"] = PHOCUS_XMP % rating
    data = dumps_phocus(d)
    info = phos_info_bytes(data)
    if info["approval"] != approval or (rating is not None and info["rating"] != rating):
        raise PhosUnsupported("新建 .phos 校验没通过")
    return data


def sha(b):
    return hashlib.sha1(b).hexdigest()


def _tmp_path(path):
    d, b = os.path.split(path)
    return os.path.join(d, ".%s.hbtmp-%s" % (b, uuid.uuid4().hex[:8]))


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write(path, data):
    """先写隐藏临时文件并落盘，再一次性替换；中途失败原文件不变。保留原文件权限。"""
    tmp = _tmp_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path) or ".")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_new_file(path, data):
    """新建文件；已存在就失败（不覆盖）。"""
    tmp = _tmp_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)  # 原子的「不存在才创建」
        except FileExistsError:
            raise
        except OSError:  # exFAT 等不支持硬链接
            if os.path.exists(path):
                raise FileExistsError(17, "已存在", path)
            os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path) or ".")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def phocus_running():
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["pgrep", "-f", "Phocus.app/Contents/MacOS/"], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


# ===================================================================== 预览图

def _jpeg_size(j):
    i = 2
    while i + 9 < len(j):
        if j[i] != 0xFF:
            return None
        m = j[i + 1]
        ln = struct.unpack(">H", j[i + 2:i + 4])[0]
        if m in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", j[i + 5:i + 9])
            return w, h
        i += 2 + ln
    return None


def _with_orientation(j, o):
    """给内嵌 JPEG 补一个只含方向的 EXIF，浏览器会自动转正。"""
    if o in (None, 0, 1) or j[:2] != b"\xff\xd8" or b"Exif\x00\x00" in j[:4096]:
        return j
    wh = _jpeg_size(j)
    if wh and o in (5, 6, 7, 8) and wh[1] > wh[0]:
        return j  # 像素已经是竖的
    tiff = b"MM\x00\x2a\x00\x00\x00\x08\x00\x01" + struct.pack(">HHIHH", 0x112, 3, 1, o, 0) + b"\x00\x00\x00\x00"
    app1 = b"\xff\xe1" + struct.pack(">H", 2 + 6 + len(tiff)) + b"Exif\x00\x00" + tiff
    return j[:2] + app1 + j[2:]


def _tiff_ifd0(f):
    f.seek(0)
    h = f.read(8)
    e = "<" if h[:2] == b"II" else ">" if h[:2] == b"MM" else None
    if not e:
        return None, {}
    f.seek(struct.unpack(e + "I", h[4:8])[0])
    n = struct.unpack(e + "H", f.read(2))[0]
    raw = f.read(12 * n)
    tags = {}
    for i in range(n):
        tag, typ, cnt = struct.unpack(e + "HHI", raw[i * 12:i * 12 + 8])
        val = raw[i * 12 + 8:i * 12 + 12]
        if cnt == 1 and typ == 3:
            tags[tag] = struct.unpack(e + "H", val[:2])[0]
        elif cnt == 1 and typ == 4:
            tags[tag] = struct.unpack(e + "I", val)[0]
    return e, tags


def raw_preview(path):
    """3FR 的 IFD0 本身就是一张 3888px JPEG（X2D II 实测），直接读出来。"""
    try:
        with open(path, "rb") as f:
            e, t = _tiff_ifd0(f)
            if t.get(0x103) not in (6, 7) or 0x111 not in t or 0x117 not in t or t[0x117] > (32 << 20):
                return None
            f.seek(t[0x111])
            j = f.read(t[0x117])
            if j[:2] != b"\xff\xd8":
                return None
            return _with_orientation(j, t.get(0x112))
    except Exception:
        return None


def heif_preview(path):
    """X2D II 的 HEIC 里带一张 3888px JPEG item，直接读出来。"""
    try:
        with open(path, "rb") as f:
            head = f.read(1 << 16)
            pos, meta = 0, None
            while pos + 8 <= len(head):
                size, typ = struct.unpack(">I4s", head[pos:pos + 8])
                if size < 8 or (typ == b"meta" and size > (2 << 20)):
                    break
                if typ == b"meta":
                    f.seek(pos + 8)
                    meta = f.read(size - 8)
                    break
                pos += size
            if not meta:
                return None
            items, locs = {}, {}
            for typ, s, e in _boxes(meta, 4, len(meta)):
                if typ == "iinf":
                    items = _parse_iinf(meta, s, e)
                elif typ == "iloc":
                    locs = _parse_iloc(meta, s, e)
            best, orient = None, None
            for iid, (itype, _) in items.items():
                if iid not in locs or locs[iid][0] != 0:
                    continue
                if itype == "jpeg":
                    off, ln = locs[iid][1][0]
                    if 0 < ln < (32 << 20) and (best is None or ln > best[1]):
                        best = (off, ln)
                elif itype == "Exif":
                    off, ln = locs[iid][1][0]
                    f.seek(off)
                    d = f.read(min(ln, 1 << 16))
                    tiff = io.BytesIO(d[4 + struct.unpack(">I", d[:4])[0]:])
                    orient = _tiff_ifd0(tiff)[1].get(0x112)
            if not best:
                return None
            f.seek(best[0])
            j = f.read(best[1])
            return _with_orientation(j, orient) if j[:2] == b"\xff\xd8" else None
    except Exception:
        return None


def render(src, dst, max_px):
    tmp = dst + ".part.jpg"
    try:
        if SIPS:
            cmd = [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "85" if max_px else "92"]
            if max_px:
                cmd += ["-Z", str(max_px)]
            r = subprocess.run(cmd + [src, "--out", tmp], stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=300)
            if r.returncode != 0 or not os.path.exists(tmp):
                raise RuntimeError("sips 失败: " + r.stderr.decode("utf-8", "replace").strip()[:200])
        else:
            from PIL import Image, ImageOps
            Image.MAX_IMAGE_PIXELS = None
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                if max_px:
                    im.thumbnail((max_px, max_px))
                im.convert("RGB").save(tmp, "JPEG", quality=85 if max_px else 92)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def long_edge(src):
    try:
        if SIPS:
            out = subprocess.run([SIPS, "-g", "pixelWidth", "-g", "pixelHeight", src],
                                 capture_output=True, timeout=30).stdout.decode("utf-8", "ignore")
            nums = [int(x) for x in re.findall(r"pixel(?:Width|Height):\s*(\d+)", out)]
            return max(nums) if nums else None
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(src) as im:
            return max(im.size)
    except Exception:
        return None


# ===================================================================== 会话

def cache_base():
    if os.environ.get("CULL3FR_CACHE"):  # 测试用
        return os.environ["CULL3FR_CACHE"]
    base = os.path.expanduser("~/Library/Caches") if sys.platform == "darwin" else tempfile.gettempdir()
    return os.path.join(base, "cull3fr")


def prune_cache(max_bytes=3 << 30, max_age=30 * 86400):
    """预览缓存：30 天没用过的删掉；总量超过 3 GB 时从最久没用的开始删。"""
    files, now = [], time.time()
    for d, _, fs in os.walk(cache_base()):
        for f in fs:
            if f.endswith(".jpg"):
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                    files.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass
    files.sort()
    total = sum(s for _, s, _ in files)
    for t, s, p in files:
        if now - t > max_age or total > max_bytes:
            try:
                os.remove(p)
                total -= s
            except OSError:
                pass


class SaveError(Exception):
    pass


class StaleRequest(Exception):
    pass


def item_key(rel_dir, stem):
    """一张照片的身份 = 所在子文件夹 + 文件名主干（不分大小写、不含扩展名），
    所以先拷 HEIC 后拷 3FR 时，标记不会丢。"""
    return (rel_dir + "/" if rel_dir not in ("", ".") else "") + stem.upper()


def _valid_r(r):
    return type(r) is int and 0 <= r <= 5


class Session:
    def __init__(self, root, preview_px=3000):
        self.root = os.path.realpath(os.path.abspath(root))
        self.preview_px = preview_px
        self.lock = threading.RLock()     # 内存里的标记和列表
        self.io_lock = threading.Lock()   # 写入 / 移动 / 撤销 一次只做一个；退出和换文件夹前要等它
        self.cache = os.path.join(cache_base(), sha(self.root.encode("utf-8"))[:12])
        os.makedirs(self.cache, exist_ok=True)
        self.sdir = self.root if os.access(self.root, os.W_OK) else self.cache
        self.state_path = os.path.join(self.sdir, STATE_FILE)
        self.log_path = os.path.join(self.sdir, LOG_FILE)
        self.backup_root = os.path.join(self.sdir, BACKUP_DIR)
        self.ov, self.written, self.carry, self.warn = {}, {}, {}, None
        self.items, self.by_key = [], {}
        self._inflight, self._ilock = {}, threading.Lock()
        self._sem = threading.Semaphore(max(2, min(4, (os.cpu_count() or 4) // 2)))
        self._fsem = threading.Semaphore(1)
        self._full_seq = 0
        self.sid = uuid.uuid4().hex[:12]
        self.rescan()
        self._load_state()

    # ---------- 路径 / 状态
    def rel(self, p):
        return os.path.relpath(p, self.root).replace(os.sep, "/")

    def apath(self, r, base=None):
        """日志里的相对路径 → 绝对路径；不允许跑出 base（默认照片文件夹）。"""
        base = os.path.realpath(base or self.root)
        p = os.path.realpath(os.path.join(base, *r.split("/")))
        if p != base and not p.startswith(base.rstrip(os.sep) + os.sep):
            raise ValueError("路径越界：" + r)
        return p

    @staticmethod
    def _migrate_key(k):
        d, _, name = k.rpartition("/")
        stem, ext = os.path.splitext(name)
        if ext.lower() in RAW_EXTS or ext.lower() in COMP_EXTS:  # v0.3 及以前用的是带扩展名的文件路径
            return item_key(d, stem)
        return k

    def _migrate_legacy(self):
        """旧版（HB Cull）留下的标记 / 撤销记录：新文件还不存在时，复制一份接着用，旧文件不动。"""
        for old, new in ((LEGACY_STATE_FILE, self.state_path), (LEGACY_LOG_FILE, self.log_path)):
            src = os.path.join(self.sdir, old)
            if os.path.exists(src) and not os.path.exists(new):
                try:
                    with open(src, "rb") as f:
                        write_new_file(new, f.read())
                except OSError:
                    pass

    def _load_state(self):
        self._migrate_legacy()
        try:
            with open(self.state_path, "rb") as f:
                d = json.loads(f.read().decode("utf-8"))
            if not isinstance(d, dict):
                raise ValueError("不是 dict")
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeDecodeError):
            bad = self.state_path + ".bad-" + time.strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(self.state_path, bad)
                self.warn = "标记文件损坏，已另存为 %s，这次从空白开始。" % os.path.basename(bad)
            except OSError:
                self.warn = "标记文件读不了。"
            return
        for k, v in (d.get("ov") or {}).items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            e = {}
            if _valid_r(v.get("r")):
                e["r"] = v["r"]
            if "l" in v and (v["l"] is None or v["l"] in APPROVAL):
                e["l"] = v["l"]
            if e:
                self.ov.setdefault(self._migrate_key(k), {}).update(e)
        for k, c in (d.get("carry") or {}).items():
            if isinstance(k, str) and isinstance(c, dict):
                e = {}
                if _valid_r(c.get("r")):
                    e["r"] = c["r"]
                if c.get("l") is None or c.get("l") in APPROVAL:
                    e["l"] = c.get("l")
                self.carry[k] = e
        for k, r in (d.get("written") or {}).items():
            if isinstance(k, str) and _valid_r(r):
                self.written[self._migrate_key(k)] = r

    def _save_state(self):
        data = json.dumps({"version": 3, "ov": self.ov, "written": self.written, "carry": self.carry},
                          ensure_ascii=False, indent=0).encode("utf-8")
        atomic_write(self.state_path, data)

    def _read_log(self):
        try:
            with open(self.log_path, "rb") as f:
                log = json.loads(f.read().decode("utf-8"))
            return [e for e in log if isinstance(e, dict) and isinstance(e.get("ops"), list)]
        except (OSError, ValueError, UnicodeDecodeError, TypeError):
            return []

    def _write_log(self, log):
        atomic_write(self.log_path, json.dumps(log, ensure_ascii=False, indent=1).encode("utf-8"))

    def _save_log_entry(self, entry):
        """按 id 更新（或追加）一条日志；没有任何操作的条目删掉。"""
        log = [e for e in self._read_log() if e.get("id") != entry["id"]]
        if entry["ops"]:
            log.append(entry)
        self._write_log(log)

    # ---------- 扫描
    def rescan(self):
        groups = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            lower = {n.lower(): n for n in filenames}
            for fn in filenames:
                if fn.startswith("."):
                    continue
                stem, ext = os.path.splitext(fn)
                e = ext.lower()
                if e not in RAW_EXTS and e not in COMP_EXTS:
                    continue
                g = groups.setdefault((dirpath, stem.upper()), {
                    "dir": dirpath, "stem": stem, "raw": None, "comp": None, "lower": lower})
                if e in RAW_EXTS:
                    if g["raw"] is None or e == ".3fr":
                        g["raw"] = fn
                elif g["comp"] is None or COMP_EXTS.index(e) < COMP_EXTS.index(
                        os.path.splitext(g["comp"])[1].lower()):
                    g["comp"] = fn
        items = []
        for g in sorted(groups.values(), key=lambda g: (self.rel(g["dir"]).lower(), g["stem"].lower())):
            src = os.path.join(g["dir"], g["comp"] or g["raw"])
            try:
                st = os.stat(src)
                v = "%d-%d" % (st.st_mtime_ns // 1000000, st.st_size)
            except OSError:
                v = "0"
            items.append({"key": item_key(self.rel(g["dir"]), g["stem"]), "name": g["stem"],
                          "dir": g["dir"], "stem": g["stem"], "raw": g["raw"], "comp": g["comp"],
                          "src": src, "v": v, "lower": g["lower"]})
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(self._read_meta, items))
        for it in items:
            it.pop("lower", None)
        with self.lock:
            self.items = items
            self.by_key = {it["key"]: it for it in items}

    def target(self, it):
        """写标记的文件：有 3FR 写 3FR；没有 RAW、只有 HEIC 的写 HEIC。FFF / 只有 JPG 的不写。"""
        if it["raw"]:
            return it["raw"] if target_kind(it["raw"]) == "3fr" else None
        if it["comp"] and target_kind(it["comp"]) == "heic":
            return it["comp"]
        return None

    def _read_meta(self, it):
        d = it["dir"]
        low = it.get("lower")
        if low is None:
            try:
                low = {n.lower(): n for n in os.listdir(d)}
            except OSError:
                low = {}
        m = {"phos": None, "phos_r": None, "in_r": None, "disk_r": None, "disk_a": 1, "heif_r": None, "err": None}
        tgt = self.target(it)
        if tgt:
            ph = low.get((tgt + ".phos").lower())
            if ph:
                m["phos"] = ph
                try:
                    info = phos_info(os.path.join(d, ph))
                    m["phos_r"] = info["rating"]
                    if info["approval"] is not None:
                        m["disk_a"] = info["approval"]
                except Exception as ex:
                    m["err"] = ".phos 读不了（%s）" % type(ex).__name__
        raw = it["raw"]
        if raw:
            rp = os.path.join(d, raw)
            try:
                with open(rp, "rb") as f:
                    m["in_r"] = tiff_rating(f)
            except OSError:
                pass
            if m["in_r"] is None:
                m["in_r"] = scan_rating(rp)
        comp = it["comp"]
        if comp:
            if not raw and m["phos"]:
                r = m["phos_r"]
            else:
                r = None
                cph = low.get((comp + ".phos").lower())
                if cph:
                    try:
                        r = phos_info(os.path.join(d, cph))["rating"]
                    except Exception:
                        r = None
            in_file = companion_rating(os.path.join(d, comp))
            if r is None:
                r = in_file
            m["heif_r"] = r
            if not raw:
                m["in_r"] = in_file
        if tgt:
            m["disk_r"] = m["phos_r"] if m["phos_r"] is not None else m["in_r"]
        elif raw:
            m["disk_r"] = m["in_r"]
        it.update(m)

    def refresh(self, keys):
        """只重读这几张的 .phos / 评分（写入后用，不必全量扫描）。"""
        with self.lock:
            its = [self.by_key[k] for k in keys if k in self.by_key]
        for it in its:
            self._read_meta(it)

    # ---------- 取值
    def is3fr(self, it):
        return bool(it["raw"]) and it["raw"].lower().endswith(".3fr")

    def file_rating(self, it):
        return (it["disk_r"] if (it["raw"] or self.target(it)) else it["heif_r"]) or 0

    def disk_label(self, it):
        return APPROVAL_INV.get(it["disk_a"]) if (self.target(it) and it["phos"]) else None

    def _carry(self, it):
        """先只有 HEIC 时用本工具写过的标记；3FR 后来到了、而且还没有自己的 .phos，就带过去（等着写进 3FR）。"""
        c = self.carry.get(it["key"])
        if c and it["raw"] and target_kind(it["raw"]) == "3fr" and not it["phos"]:
            return c
        return None

    def _desired(self, it):
        d = dict(self._carry(it) or {})
        d.update(self.ov.get(it["key"], {}))
        return d

    def eff(self, it):
        ov = self._desired(it)
        return (ov["r"] if "r" in ov else self.file_rating(it),
                ov["l"] if "l" in ov else self.disk_label(it))

    def mismatch(self, it):
        # HEIF 完全没有评分信息时不算不一致；HEIF 有评分（包括 0）且和 RAW 不同才算
        if not (it["raw"] and it["comp"]) or it["heif_r"] is None or self._carry(it):
            return None
        a, b = it["disk_r"] or 0, it["heif_r"]
        if a == b or self.written.get(it["key"]) == a:
            return None
        return [a, b]

    def _want(self, it):
        """只写用户在本工具里改过的值；返回 (星级或 None, Approval 或 None)。"""
        ov = self._desired(it)
        if not ov or not self.target(it):
            return None, None
        r = ov["r"] if "r" in ov and ov["r"] != (it["disk_r"] or 0) else None
        a = None
        if "l" in ov and APPROVAL.get(ov["l"], 1) != it["disk_a"]:
            a = APPROVAL.get(ov["l"], 1)
        return r, a

    def pending(self, it):
        return self._want(it) != (None, None)

    def kind(self, it):
        parts = []
        if it["comp"]:
            parts.append(os.path.splitext(it["comp"])[1][1:].upper())
        if it["raw"]:
            parts.append(os.path.splitext(it["raw"])[1][1:].upper())
        return " + ".join(parts)

    def item_json(self, it):
        r, l = self.eff(it)
        return {"k": it["key"], "n": it["name"], "kind": self.kind(it), "v": it["v"],
                "r": r if _valid_r(r) else 0, "l": l if l in APPROVAL else None,
                "mm": self.mismatch(it), "p": self.pending(it), "err": it["err"]}

    def items_json(self):
        with self.lock:
            return {"name": os.path.basename(self.root), "root": self.root, "sid": self.sid, "warn": self.warn,
                    "items": [self.item_json(it) for it in self.items]}

    # ---------- 打标
    def set_mark(self, key, body):
        with self.lock:
            it = self.by_key.get(key)
            if not it:
                return None
            before = dict(self.ov[key]) if key in self.ov else None
            ov = self.ov.setdefault(key, {})
            if "r" in body:
                if not _valid_r(body["r"]):
                    raise ValueError("星级只能是 0 到 5")
                if body["r"] == self.file_rating(it):
                    ov.pop("r", None)
                else:
                    ov["r"] = body["r"]
            if "l" in body:
                l = body["l"]
                if l is not None and l not in APPROVAL:
                    raise ValueError("颜色只能是绿黄红")
                if l == self.disk_label(it):
                    ov.pop("l", None)
                else:
                    ov["l"] = l
            if not ov:
                self.ov.pop(key, None)
            try:
                self._save_state()
            except OSError as ex:
                if before is None:
                    self.ov.pop(key, None)
                else:
                    self.ov[key] = before
                raise SaveError("标记文件写不进去（%s）" % (ex.strerror or ex))
            return self.item_json(it)

    # ---------- 预览
    def cache_path(self, it, kind):
        tag = "%s|%s|%s" % (it["src"], it["v"], self.preview_px if kind == "p" else "full")
        return os.path.join(self.cache, sha(tag.encode("utf-8"))[:20] + "_" + kind + ".jpg")

    def ensure(self, it, kind):
        out = self.cache_path(it, kind)
        my_seq = None
        if kind == "f":
            with self._ilock:
                self._full_seq += 1
                my_seq = self._full_seq
        while True:
            if os.path.exists(out):
                try:
                    os.utime(out, None)  # 记录最近使用，清缓存时按这个排
                except OSError:
                    pass
                return out
            with self._ilock:
                ev = self._inflight.get(out)
                owner = ev is None
                if owner:
                    ev = threading.Event()
                    ev.stale = False
                    self._inflight[out] = ev
            if not owner:
                ev.wait()
                if os.path.exists(out):
                    continue
                if ev.stale:
                    continue  # 前一个同样的请求因为「已翻走」被放弃了，这次由我来算
                raise RuntimeError("预览生成失败")
            try:
                with (self._sem if kind == "p" else self._fsem):
                    if kind == "f" and my_seq != self._full_seq:
                        ev.stale = True
                        raise StaleRequest("已经翻到别的照片了")  # 用户已经翻走，不再算这张 1 亿像素的大图
                    j = None
                    if kind == "p":
                        if it["comp"] and os.path.splitext(it["comp"])[1].lower() in (".heif", ".heic", ".hif"):
                            j = heif_preview(os.path.join(it["dir"], it["comp"]))
                        if j is None and it["raw"]:
                            j = raw_preview(os.path.join(it["dir"], it["raw"]))
                    if j is not None:
                        atomic_write(out, j)
                    else:
                        render(it["src"], out, self.preview_px if kind == "p" else None)
            finally:
                with self._ilock:
                    self._inflight.pop(out, None)
                ev.set()
            return out

    def dims(self, it):
        if "long" not in it:
            it["long"] = long_edge(it["src"])
        return it["long"]

    # ---------- 写入 Phocus
    def _template(self, kind):
        """新建 .phos 用的基线：优先用这个文件夹里 Phocus 写过的同类 .phos。"""
        with self.lock:
            cands = [(it["dir"], it["phos"]) for it in self.items
                     if it["phos"] and target_kind(self.target(it) or "") == kind]
        for d, ph in cands[:20]:
            try:
                with open(os.path.join(d, ph), "rb") as f:
                    t = phos_template_from(f.read(), kind)
                if t:
                    return t, "文件夹里的 " + ph
            except Exception:
                pass
        return embedded_template(kind), "内置模板（Phocus 4.x，.phos Version 31）"

    def _targets(self):
        out, blocked = [], {"fff": [], "no_raw": [], "err": []}
        for it in self.items:
            if it["key"] not in self.ov and not self._carry(it):
                continue
            tgt = self.target(it)
            if not tgt:
                blocked["fff" if it["raw"] else "no_raw"].append(it["name"])
                continue
            r, a = self._want(it)
            if r is None and a is None:
                continue
            if it["err"]:
                blocked["err"].append("%s（%s）" % (it["name"], it["err"]))
                continue
            out.append({"key": it["key"], "name": it["name"], "dir": it["dir"], "target": tgt,
                        "kind": target_kind(tgt), "phos": it["phos"], "in_r": it["in_r"], "r": r, "a": a})
        return out, blocked

    def plan(self):
        with self.lock:
            targets, blocked = self._targets()
        n_new = sum(1 for t in targets if not t["phos"])
        n_new_heic = sum(1 for t in targets if not t["phos"] and t["kind"] == "heic")
        n_r = sum(1 for t in targets if t["r"] is not None)
        n_a = sum(1 for t in targets if t["a"] is not None)
        if targets:
            lines = ["将写入 %d 张：星级 %d 张，颜色 %d 张。" % (len(targets), n_r, n_a)]
            if n_new:
                lines.append("其中 %d 张还没有 .phos，会新建一个（和 Phocus 只打标、不调参数时写的结构相同）。" % n_new)
            if n_new_heic:
                lines.append("⚠ 其中 %d 张是只有 HEIC 的照片：新建的 HEIC .phos 里色温写 0（Phocus 原本按图算出一个值），"
                             "写完请在 Phocus 里看一眼颜色有没有变。" % n_new_heic)
        else:
            lines = ["没有需要写入的改动。"]
        lines += _blocked_lines(blocked, [])
        return {"files": len(targets), "text": "\n".join(lines), "phocus": phocus_running(),
                "canUndo": bool(self._read_log())}

    def _build(self, t, tpl):
        """算出新内容（不写盘）。返回 (路径, 旧内容或 None, 新内容或 None, 星级是否写入)。"""
        if t["phos"]:
            p = os.path.join(t["dir"], t["phos"])
            with open(p, "rb") as f:
                old = f.read()
            try:
                return p, old, phos_patch(old, t["r"], t["a"]), t["r"] is not None
            except RatingUnsupported:
                if t["a"] is None:
                    return p, old, None, False
                return p, old, phos_patch(old, None, t["a"]), False
        p = os.path.join(t["dir"], t["target"] + ".phos")
        keep_r = t["r"] if t["r"] is not None else (t["in_r"] or None)  # 保留机内星级
        return p, None, phos_new(tpl[t["kind"]], t["a"] if t["a"] is not None else 1, keep_r, t["kind"]), \
            t["r"] is not None

    def _busy(self):
        return {"ok": False, "text": "上一个操作还没做完，稍等再点。", "canUndo": bool(self._read_log())}

    def write_all(self, force=False):
        if phocus_running() and not force:
            return {"ok": False, "needForce": True}
        if not self.io_lock.acquire(timeout=1):
            return self._busy()
        try:
            return self._write_all()
        finally:
            self.io_lock.release()

    def _write_all(self):
        with self.lock:
            targets, blocked = self._targets()
        kinds = {t["kind"] for t in targets if not t["phos"]}
        tpl, tpl_src = {}, []
        for k in kinds:
            tpl[k], src_name = self._template(k)
            tpl_src.append(src_name)
        tpl_src = "；".join(sorted(tpl_src))
        bad, no_rating, prepared, bdir = [], [], [], None
        # 第 1 步：算好新内容，给要改的旧 .phos 做备份（还没动任何 .phos）
        for t in targets:
            try:
                p, old, new, r_ok = self._build(t, tpl)
                if t["r"] is not None and not r_ok:
                    no_rating.append(t["name"])
                if new is None:
                    continue
                op = {"op": "phos" if old is not None else "phos_new", "path": self.rel(p), "sha": sha(new),
                      "key": t["key"], "name": t["name"], "r": t["r"] if r_ok else None, "a": t["a"],
                      "kind": t["kind"]}
                if old is not None:
                    if bdir is None:
                        os.makedirs(self.backup_root, exist_ok=True)
                        bdir = tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=self.backup_root)
                    bk = os.path.join(bdir, *op["path"].split("/"))
                    os.makedirs(os.path.dirname(bk), exist_ok=True)
                    write_new_file(bk, old)
                    op.update(backup=os.path.relpath(bk, self.sdir).replace(os.sep, "/"), sha_old=sha(old))
                prepared.append((op, new))
            except Exception as ex:
                bad.append("%s（%s）" % (t["name"], ex))
        # 第 2 步：先记账（撤销日志），再逐个写；写之前确认文件没被别的程序改过
        entry = {"id": uuid.uuid4().hex, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "phos",
                 "ops": [op for op, _ in prepared]}
        if prepared:
            self._save_log_entry(entry)
        done, keys = [], set()
        for op, new in prepared:
            p = os.path.join(self.root, *op["path"].split("/"))
            try:
                if op["op"] == "phos":
                    with open(p, "rb") as f:
                        if sha(f.read()) != op["sha_old"]:
                            bad.append("%s（.phos 刚被别的程序改过，跳过）" % op["name"])
                            continue
                    atomic_write(p, new)
                else:
                    write_new_file(p, new)
                done.append(op)
                keys.add(op["key"])
                with self.lock:
                    if op["r"] is not None:
                        self.written[op["key"]] = op["r"]
                    it = self.by_key.get(op["key"])
                    if op["kind"] == "heic" and it:
                        # 记下写进 HEIC 的结果：以后 3FR 到了会带过去
                        r_eff, l_eff = self.eff(it)
                        op["carry_before"] = self.carry.get(op["key"])
                        self.carry[op["key"]] = {"r": r_eff, "l": l_eff}
                    elif op["key"] in self.carry:
                        op["carry_before"] = self.carry.pop(op["key"])
                    # 写成功后，工具里的这条标记让位给文件本身：之后在 Phocus 里改的不会被下次写入盖掉。
                    # 原标记记进日志，撤销时放回来。
                    ov = self.ov.get(op["key"])
                    if ov:
                        op["ov"] = dict(ov)
                        if op["r"] is not None and ov.get("r") == op["r"]:
                            ov.pop("r")
                        if op.get("a") is not None and "l" in ov and APPROVAL.get(ov["l"], 1) == op["a"]:
                            ov.pop("l")
                        if not ov:
                            self.ov.pop(op["key"], None)
            except FileExistsError:
                bad.append("%s（同名 .phos 已经存在，跳过）" % op["name"])
            except OSError as ex:
                bad.append("%s（%s）" % (op["name"], ex.strerror or ex))
        if prepared:
            entry["ops"] = done
            self._save_log_entry(entry)
        self.refresh(keys)
        with self.lock:
            try:
                self._save_state()
            except OSError:
                pass
        n_new = sum(1 for op in done if op["op"] == "phos_new")
        lines = ["已写入 %d 张（改已有 .phos %d，新建 %d）。" % (len(done), len(done) - n_new, n_new)]
        if n_new:
            lines.append("新建 .phos 用的基线：%s。" % tpl_src)
        if no_rating:
            lines.append("· %d 张的星级没写（.phos 里的星级字段不认识）：%s" % (len(no_rating), _names(no_rating)))
        if bdir and any(op["op"] == "phos" for op in done):
            lines.append("改之前的 .phos 备份在 %s" % bdir)
        lines += _blocked_lines(blocked, bad)
        return {"ok": True, "text": "\n".join(lines), "canUndo": bool(self._read_log())}

    # ---------- 按颜色分文件夹
    def move_to_folders(self):
        if not self.io_lock.acquire(timeout=1):
            return self._busy()
        try:
            return self._move()
        finally:
            self.io_lock.release()

    def _move(self):
        plan, listing = [], {}
        with self.lock:
            for it in self.items:
                lab = self.eff(it)[1]
                if lab not in OUT_DIRS:
                    continue
                dest = os.path.join(self.root, OUT_DIRS[lab])
                if os.path.realpath(it["dir"]) == dest:
                    continue
                if it["dir"] not in listing:
                    listing[it["dir"]] = os.listdir(it["dir"])
                pre = it["stem"].upper() + "."
                pairs = [(os.path.join(it["dir"], f), os.path.join(dest, f))
                         for f in listing[it["dir"]] if f.upper().startswith(pre)]
                plan.append((it, dest, lab, pairs))
        entry = {"id": uuid.uuid4().hex, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "folders", "ops": []}
        moved, bad = {"green": 0, "yellow": 0, "red": 0}, []
        try:
            for n, (it, dest, lab, pairs) in enumerate(plan):
                if os.path.exists(dest) and not os.path.isdir(dest):
                    bad.append("%s（%s 不是文件夹）" % (it["name"], OUT_DIRS[lab]))
                    continue
                if any(os.path.exists(d) for _, d in pairs):
                    bad.append("%s（目标文件夹已有同名文件）" % it["name"])
                    continue
                try:
                    os.makedirs(dest, exist_ok=True)
                except OSError as ex:
                    bad.append("%s（%s）" % (it["name"], ex.strerror or ex))
                    continue
                done = []
                try:
                    for s, d in pairs:
                        os.rename(s, d)
                        done.append((s, d))
                except OSError as ex:
                    for s, d in reversed(done):
                        try:
                            os.rename(d, s)
                        except OSError:
                            entry["ops"].append({"op": "move", "src": self.rel(s), "dst": self.rel(d)})
                    bad.append("%s（%s，已还原）" % (it["name"], ex.strerror or ex))
                    continue
                newkey = item_key(self.rel(dest), it["stem"])
                entry["ops"] += [{"op": "move", "src": self.rel(s), "dst": self.rel(d)} for s, d in done]
                entry["ops"].append({"op": "rekey", "old": it["key"], "new": newkey})
                with self.lock:
                    self._rekey(it["key"], newkey)
                moved[lab] += 1
                if n % 25 == 24:
                    self._save_log_entry(entry)
        finally:
            self._save_log_entry(entry)
            with self.lock:
                try:
                    self._save_state()
                except OSError:
                    pass
            self.rescan()
        lines = ["已移动：绿 %d · 黄 %d · 红 %d" % (moved["green"], moved["yellow"], moved["red"])]
        if bad:
            lines.append("跳过 %d 张：%s" % (len(bad), _names(bad)))
        return {"ok": True, "text": "\n".join(lines), "canUndo": bool(self._read_log())}

    def _restore_marks(self, op):
        """文件真的回到写入前了，才把当时让位的标记放回来（不然会把之后在 Phocus 里的修改又盖掉）。"""
        key = op.get("key")
        if not key:
            return
        with self.lock:
            if isinstance(op.get("ov"), dict):
                cur_ov = self.ov.setdefault(key, {})
                for k2, v2 in op["ov"].items():
                    cur_ov.setdefault(k2, v2)
            if "carry_before" in op:
                if op["carry_before"]:
                    self.carry[key] = op["carry_before"]
                else:
                    self.carry.pop(key, None)

    def _rekey(self, old, new):
        if old in self.ov:
            self.ov[new] = self.ov.pop(old)
        if old in self.written:
            self.written[new] = self.written.pop(old)
        if old in self.carry:
            self.carry[new] = self.carry.pop(old)

    # ---------- 撤销
    def undo(self):
        if not self.io_lock.acquire(timeout=1):
            return self._busy()
        try:
            return self._undo()
        finally:
            self.io_lock.release()

    def _undo(self):
        log = self._read_log()
        if not log:
            return {"ok": False, "text": "没有可以撤销的操作。", "canUndo": False}
        entry = log.pop()
        restored, skipped, retry, dropped, keys = 0, [], [], [], set()

        def read(p):
            with open(p, "rb") as f:
                return f.read()
        for op in reversed(entry["ops"]):
            try:
                kind = op.get("op")
                if kind == "move":
                    s, d = self.apath(op["src"]), self.apath(op["dst"])
                    if os.path.exists(d) and not os.path.exists(s):
                        os.makedirs(os.path.dirname(s), exist_ok=True)
                        os.rename(d, s)
                        restored += 1
                    elif not (os.path.exists(s) and not os.path.exists(d)):
                        skipped.append(op["dst"])
                elif kind == "rekey":
                    with self.lock:
                        self._rekey(op["new"], op["old"])
                elif kind == "phos":
                    p = self.apath(op["path"])
                    cur = sha(read(p))
                    back = False
                    if cur == op["sha"]:
                        data = read(self.apath(op["backup"], self.sdir))
                        if op.get("sha_old") and sha(data) != op["sha_old"]:
                            raise ValueError("备份文件和记录对不上")
                        atomic_write(p, data)
                        restored += 1
                        back = True
                    elif cur == op.get("sha_old"):
                        back = True
                    else:
                        skipped.append(op["path"] + "（之后又被改过，保留现状）")
                    keys.add(op.get("key"))
                    if back:
                        self._restore_marks(op)
                elif kind == "phos_new":
                    p = self.apath(op["path"])
                    back = True
                    if os.path.exists(p):
                        if sha(read(p)) == op["sha"]:
                            os.remove(p)
                            restored += 1
                        else:
                            skipped.append(op["path"] + "（之后被 Phocus 改过，保留）")
                            back = False
                    keys.add(op.get("key"))
                    if back:
                        self._restore_marks(op)
            except (FileNotFoundError, ValueError, KeyError) as ex:  # 备份没了、路径越界等：重试也没用
                dropped.append("%s（%s）" % (op.get("path") or op.get("dst"), getattr(ex, "strerror", None) or ex))
            except OSError as ex:
                (retry if entry.get("retries", 0) < 1 else dropped).append(
                    op if entry.get("retries", 0) < 1 else "%s（%s）" % (op.get("path") or op.get("dst"), ex.strerror or ex))
        if retry:  # 临时性错误：留在日志里，可以再点一次撤销重试（只重试一次，不会卡住更早的记录）
            log.append({"id": uuid.uuid4().hex, "time": entry.get("time"), "mode": entry.get("mode"),
                        "ops": retry[::-1], "retries": entry.get("retries", 0) + 1})
        self._write_log(log)
        with self.lock:
            for k in keys:
                self.written.pop(k, None)
            try:
                self._save_state()
            except OSError:
                pass
        if entry.get("mode") == "folders":
            for d in OUT_DIRS.values():
                try:
                    os.rmdir(os.path.join(self.root, d))
                except OSError:
                    pass
            self.rescan()
        else:
            self.refresh(keys)
        what = "写入" if entry.get("mode") == "phos" else "移动"
        if restored or not retry:
            text = "已撤销 %s 的%s：还原 %d 个文件。" % (entry.get("time"), what, restored)
        else:
            text = "%s 的%s这次没有撤销成功。" % (entry.get("time"), what)
        if skipped:
            text += "\n没动 %d 项：%s" % (len(skipped), _names(skipped))
        if retry:
            text += "\n有 %d 项暂时没还原成功，再点一次撤销会重试。" % len(retry)
        if dropped:
            text += "\n%d 项撤销不了，已从撤销记录里移除：%s" % (len(dropped), _names(dropped))
            if entry.get("mode") == "phos":
                text += "\n原来的 .phos 备份在 %s" % self.backup_root
        return {"ok": True, "text": text, "canUndo": bool(log)}


def _names(names):
    return "、".join(names[:8]) + (" 等" if len(names) > 8 else "")


def _blocked_lines(blocked, bad):
    out = []
    if bad:
        out.append("· %d 张没写成，跳过：%s" % (len(bad), _names(bad)))
    if blocked.get("err"):
        out.append("· %d 张的 .phos 读不了，不碰：%s" % (len(blocked["err"]), _names(blocked["err"])))
    if blocked.get("fff"):
        out.append("· %d 张是 FFF（标签存在文件内部），不写：%s" % (len(blocked["fff"]), _names(blocked["fff"])))
    if blocked.get("no_raw"):
        out.append("· %d 张只有 JPG，没有 3FR，不写：%s" % (len(blocked["no_raw"]), _names(blocked["no_raw"])))
    return out


# ===================================================================== 网页

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cull3fr</title>
<style>
:root{--bg:#0e0e0e;--panel:#171717;--line:#2a2a2a;--fg:#e8e8e8;--mut:#8b8b8b;--g:#34c759;--y:#ffcc00;--r:#ff453a;--o:#ff9f0a}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;overflow:hidden;-webkit-user-select:none;user-select:none}
#stage,#zoom{position:absolute;left:0;right:0;top:0;bottom:66px}
#stage{display:flex;align-items:center;justify-content:center;padding:22px}
#pic{max-width:100%;max-height:100%;display:block;cursor:zoom-in;outline:4px solid transparent;outline-offset:5px;transition:opacity .12s}
#pic.green{outline-color:var(--g)}#pic.yellow{outline-color:var(--y)}#pic.red{outline-color:var(--r)}
#pic.wait{opacity:.35}
#msg{display:none;color:var(--mut);font-size:15px}
#zoom{overflow:auto;display:none;cursor:grab;background:#000}
#zoom.drag{cursor:grabbing}
#zimg{display:block;margin:0 auto;max-width:none}
#ov{position:absolute;top:10px;left:14px;z-index:4;font-size:20px;letter-spacing:2px;text-shadow:0 1px 3px #000;pointer-events:none}
#ov .s{color:var(--y)}#ov .e{color:rgba(255,255,255,.25)}
#ov .mmb{font-size:12px;letter-spacing:0;margin-left:10px;color:#000;background:var(--o);border-radius:4px;padding:2px 6px;vertical-align:3px;text-shadow:none}
#badge{position:absolute;top:12px;left:50%;transform:translateX(-50%);background:rgba(20,20,20,.85);padding:5px 12px;border-radius:14px;font-size:12px;display:none;z-index:6}
#bar{position:absolute;left:0;right:0;bottom:0;height:66px;background:var(--panel);border-top:1px solid var(--line)}
#strip{display:flex;height:12px;cursor:pointer;position:relative}
#strip i{flex:1 1 0;min-width:0;background:#2e2e2e}
#strip.sep i{border-right:1px solid #111}
#strip i.rated{background:#5a5a5a}#strip i.green{background:var(--g)}#strip i.yellow{background:var(--y)}#strip i.red{background:var(--r)}
#strip i.dim{opacity:.18}
#mark{position:absolute;top:0;width:3px;height:12px;background:#fff;transform:translateX(-50%);pointer-events:none}
#row{display:flex;align-items:center;gap:14px;height:54px;padding:0 14px;white-space:nowrap}
#name{font-weight:600;font-size:14px}
.tag{color:var(--mut);font-size:11px;border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.mut{color:var(--mut)}
#stars{font-size:15px;letter-spacing:1px}#stars .s{color:var(--y)}#stars .e{color:#444}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.dot.green{background:var(--g)}.dot.yellow{background:var(--y)}.dot.red{background:var(--r)}
#mm{color:var(--o)}
.sp{flex:1}
#keys{color:var(--mut);font-size:12px}
kbd{font:11px ui-monospace,Menlo,monospace;border:1px solid #3a3a3a;border-bottom-width:2px;border-radius:4px;padding:0 4px;margin:0 2px;color:#ccc}
#counts{color:var(--mut);font-size:12px}
button{background:#262626;color:var(--fg);border:1px solid #363636;border-radius:7px;padding:6px 11px;font:inherit;cursor:pointer}
button:hover{background:#303030}button:disabled{opacity:.4;cursor:default}
button.pri{background:#0a84ff;border-color:#0a84ff;color:#fff}button.pri:hover{background:#2b95ff}
button.pend{background:var(--o);border-color:var(--o);color:#000}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:10}
.modal.show{display:flex}
.card{background:#1c1c1c;border:1px solid #333;border-radius:12px;padding:20px 22px;width:min(600px,92vw);max-height:88vh;overflow:auto;-webkit-user-select:text;user-select:text}
.card h2{margin:0 0 12px;font-size:16px}
.txt{white-space:pre-wrap;font-size:13px;line-height:1.6;color:#d5d5d5}
.warn{color:var(--o);margin:8px 0}
.btns{display:flex;gap:8px;margin-top:14px}.btns.end{justify-content:flex-end}
details{margin-top:14px;color:#bbb}details p{font-size:12px;color:#999}
.help td{padding:3px 12px 3px 0;vertical-align:top}.help td:first-child{white-space:nowrap}
#fold{max-width:220px;overflow:hidden;text-overflow:ellipsis}
@media (max-width:1300px){#counts{display:none}}
@media (max-width:1100px){#fold{max-width:120px}}
@media (max-width:950px){#kind,#lab{display:none}}
</style></head><body>
<div id="stage"><img id="pic" alt=""><div id="msg"></div></div>
<div id="zoom"><img id="zimg" alt=""></div>
<div id="ov"></div><div id="badge"></div>
<div id="bar">
  <div id="strip"></div><div id="mark"></div>
  <div id="row">
    <button id="fold" title="换文件夹（O）">📁 <span id="fname"></span></button>
    <span id="name"></span><span id="kind" class="tag"></span><span id="pos" class="mut"></span>
    <span id="stars"></span><span id="lab"></span><span id="mm"></span>
    <span class="sp"></span>
    <span id="counts"></span>
    <button id="fbtn"></button><button id="autob"></button><button id="hbtn">? 快捷键</button>
    <button id="wbtn" class="pri">全部写入</button><button id="qbtn">退出</button>
  </div>
</div>

<div class="modal" id="wdlg"><div class="card">
  <h2>全部写入 Phocus</h2>
  <div id="plan" class="txt">读取中…</div>
  <div id="warn" class="warn"></div>
  <div class="btns"><button id="wgo" class="pri">写入 .phos</button><button id="wforce">Phocus 开着也照样写</button></div>
  <div id="wres" class="txt" style="margin-top:12px"></div>
  <details><summary>另一种方式：按颜色分文件夹</summary>
    <p>把有颜色的照片（3FR、同名 HEIF/JPG、.phos 一起）移到这个文件夹里的 _green / _yellow / _red。没颜色的不动。黄色在 Phocus 里和未标看不出区别，需要单独拿出黄色时用这个。</p>
    <button id="fgo">移动到颜色文件夹</button></details>
  <div class="btns end"><button id="wundo">撤销上一次写入/移动</button><button class="close">关闭</button></div>
</div></div>

<div class="modal" id="hdlg"><div class="card help">
  <h2>快捷键</h2>
  <table>
  <tr><td><kbd>→</kbd> <kbd>←</kbd></td><td>下一张 / 上一张</td></tr>
  <tr><td><kbd>1</kbd> … <kbd>5</kbd></td><td>评分，<kbd>0</kbd> 清除评分</td></tr>
  <tr><td><kbd>6</kbd> <kbd>7</kbd> <kbd>8</kbd></td><td>绿 / 黄 / 红（和 Phocus 一样；也可以用 <kbd>G</kbd> <kbd>Y</kbd> <kbd>R</kbd>），<kbd>9</kbd> 清除颜色</td></tr>
  <tr><td><kbd>⇧</kbd> + 上面的键</td><td>打标但不跳下一张（方便同一张既打星又打颜色）</td></tr>
  <tr><td><kbd>A</kbd></td><td>自动下一张 开 / 关</td></tr>
  <tr><td><kbd>空格</kbd> 或单击</td><td>100% 查看；拖动平移；放大时翻页保持同一位置，方便比连拍焦点</td></tr>
  <tr><td><kbd>F</kbd> / <kbd>⇧F</kbd></td><td>筛选：全部 → 未标 → 绿 → 黄 → 红 → 1★+ → 3★+ → 不一致 → 未写入</td></tr>
  <tr><td><kbd>E</kbd></td><td>全部写入</td></tr>
  <tr><td><kbd>O</kbd></td><td>换文件夹</td></tr>
  </table>
  <p class="txt mut">每次打标都自动存到文件夹里的 .cull3fr.json，关掉再开会接着上次。照片文件只在「全部写入」时才改（只改 .phos，写前备份）。<br>「不一致」：同名 HEIF 和 RAW 的评分不同，已按 RAW 取值。</p>
  <h2 style="margin-top:18px">以后不用终端</h2>
  <p class="txt mut">点下面的按钮会生成一个「Cull3fr」App（放在你的「应用程序」文件夹里）。以后用 Spotlight 搜 Cull3fr 或从 Dock 双击打开，选文件夹就开始；用完点右下角「退出」。cull3fr.py 生成后不要再挪位置。</p>
  <div class="btns"><button id="inst">生成 Cull3fr App</button></div>
  <div id="instres" class="txt" style="margin-top:8px"></div>
  <div class="btns end"><button class="close">关闭</button></div>
</div></div>

<div class="modal" id="qdlg"><div class="card">
  <h2>退出 cull3fr</h2>
  <p class="txt">打的标记已经自动保存，下次打开这个文件夹会接着上次。</p>
  <div id="qwarn" class="warn"></div>
  <div class="btns end"><button class="close">取消</button><button id="qgo" class="pri">退出</button></div>
</div></div>

<div class="modal" id="lost"><div class="card">
  <h2>和本地工具断开了</h2>
  <p class="txt" id="lostmsg"></p>
  <div class="btns end"><button id="retry" class="pri">重新连接</button></div>
</div></div>

<div class="modal" id="bye"><div class="card">
  <h2>已退出</h2>
  <p class="txt">可以关掉这个标签页了。下次从「Cull3fr」App 打开（还没生成的话，打开后在「? 快捷键」里点「生成 Cull3fr App」）。</p>
</div></div>

<script>
const TOKEN="__CULL3FR_TOKEN__";
const $=s=>document.querySelector(s);
const pic=$('#pic'),zoomEl=$('#zoom'),zimg=$('#zimg'),stage=$('#stage'),msg=$('#msg'),badge=$('#badge'),strip=$('#strip'),mark=$('#mark'),ov=$('#ov');
const LBL={green:'绿',yellow:'黄',red:'红'};
const FILTERS=[['all','全部'],['unmarked','未标'],['green','绿'],['yellow','黄'],['red','红'],['star1','1★+'],['star3','3★+'],['mismatch','不一致'],['pending','未写入']];
let items=[],cur=-1,fi=0,zoom=null,mouse=null,tok=0,auto=true,badgeT=0,sid='',quitted=false,keySeq=0,lost=false;
try{auto=localStorage.getItem('cull3fr_auto')!=='0'}catch(e){}
const F=()=>FILTERS[fi][0];
const enc=encodeURIComponent;
const pURL=it=>`/img/p?k=${enc(it.k)}&v=${enc(it.v)}&t=${TOKEN}`, fURL=it=>`/img/f?k=${enc(it.k)}&v=${enc(it.v)}&t=${TOKEN}`;
const safeL=l=>LBL[l]?l:null;
function match(i){const x=items[i];switch(F()){case'all':return true;case'unmarked':return !x.l&&!x.r;case'star1':return x.r>=1;case'star3':return x.r>=3;case'mismatch':return !!x.mm;case'pending':return x.p;default:return x.l===F()}}
function step(from,dir){for(let i=from+dir;i>=0&&i<items.length;i+=dir)if(match(i))return i;return -1}
async function get(u){const r=await fetch(u,{headers:{'X-Cull3fr-Token':TOKEN}});if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}
async function api(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-Cull3fr-Token':TOKEN},body:JSON.stringify(Object.assign({sid},b||{}))});if(!r.ok&&r.status!==500)throw new Error('HTTP '+r.status);return r.json()}
// 徽标：文字一律用 textContent；只有内部拼的星星/色点才用 HTML
function showBadge(text,ms){badge.textContent=text;badge.style.display='block';clearTimeout(badgeT);if(ms)badgeT=setTimeout(()=>badge.style.display='none',ms)}
function showBadgeHTML(html,ms){badge.innerHTML=html;badge.style.display='block';clearTimeout(badgeT);if(ms)badgeT=setTimeout(()=>badge.style.display='none',ms)}
function hideBadge(){badge.style.display='none'}
const stars=r=>{r=Math.max(0,Math.min(5,r|0));return '<span class="s">'+'★'.repeat(r)+'</span><span class="e">'+'★'.repeat(5-r)+'</span>'};

async function load(keepKey,keepName,guardSeq){
  const d=await get('/api/items');
  if(guardSeq!==undefined&&guardSeq!==keySeq)return;  // 等数据的时候用户又按了键：不打断
  if(keepKey===undefined&&cur>=0&&items[cur]){keepKey=items[cur].k;keepName=items[cur].n}
  const prev=cur>=0&&items[cur]?{k:items[cur].k,v:items[cur].v}:null;
  items=d.items;sid=d.sid;document.title='cull3fr · '+d.name;$('#fname').textContent=d.name;$('#fold').title=d.root+'（点这里换文件夹，快捷键 O）';
  strip.innerHTML=items.map(()=>'<i></i>').join('');strip.classList.toggle('sep',items.length<250);
  paintStrip();
  let i=keepKey?items.findIndex(x=>x.k===keepKey):-1;
  if(i<0&&keepName)i=items.findIndex(x=>x.n===keepName);
  if(i<0){i=items.findIndex(x=>!x.l&&!x.r);if(i<0)i=0}
  if(!match(i)){const n=step(i,1);i=n>=0?n:step(i,-1)}
  if(prev&&i>=0&&items[i].k===prev.k&&items[i].v===prev.v&&pic.naturalWidth){cur=i;updBar()}  // 还是同一张：不重新加载图片
  else show(i);
  if(d.warn)showBadge('⚠ '+d.warn,8000);
}
function cellClass(i){const x=items[i];return (safeL(x.l)||(x.r?'rated':''))+(match(i)?'':' dim')}
function paintStrip(){const c=strip.children;for(let i=0;i<items.length;i++)c[i].className=cellClass(i)}
function paintCell(i){if(strip.children[i])strip.children[i].className=cellClass(i)}

function show(i){
  cancelFull();
  if(i<0||i>=items.length){cur=-1;pic.style.display='none';msg.textContent=`没有「${FILTERS[fi][1]}」的照片`;msg.style.display='block';exitZoom();updBar();return}
  cur=i;const it=items[i],t=++tok,url=pURL(it);msg.style.display='none';
  const slow=setTimeout(()=>{if(t===tok){pic.classList.add('wait');showBadge('加载中…')}},150);
  const im=new Image();
  im.onload=()=>{clearTimeout(slow);if(t!==tok)return;pic.src=url;pic.style.display='block';pic.classList.remove('wait');hideBadge();if(zoom)enterZoom(zoom.fx,zoom.fy,null);prefetch(i)};
  im.onerror=()=>{clearTimeout(slow);if(t!==tok)return;pic.style.display='none';pic.classList.remove('wait');msg.textContent='无法预览：'+it.n;msg.style.display='block';hideBadge();prefetch(i)};
  im.src=url;updBar();
}
const pre=new Map();
function prefetch(i){const want=[];let j=i;for(let n=0;n<3;n++){j=step(j,1);if(j<0)break;want.push(j)}const p=step(i,-1);if(p>=0)want.push(p);
  for(const k of want){const u=pURL(items[k]);if(pre.has(u))continue;const im=new Image();im.src=u;pre.set(u,im);if(pre.size>30)pre.delete(pre.keys().next().value)}}

function updBar(){
  const it=cur>=0?items[cur]:null;let tot=0,rank=0,g=0,y=0,r=0,s=0,mm=0,pend=0;
  for(let i=0;i<items.length;i++){const x=items[i];if(match(i)){tot++;if(i<=cur)rank++}if(x.l==='green')g++;else if(x.l==='yellow')y++;else if(x.l==='red')r++;if(x.r)s++;if(x.mm)mm++;if(x.p)pend++}
  $('#name').textContent=it?it.n:'';$('#kind').textContent=it?it.kind:'';$('#kind').style.display=it?'':'none';
  $('#pos').textContent=(it&&match(cur)?rank:'-')+' / '+tot+(F()!=='all'?`（共 ${items.length}）`:'');
  $('#stars').innerHTML=it?stars(it.r):'';
  const l=it?safeL(it.l):null;
  $('#lab').innerHTML=it?(l?`<span class="dot ${l}"></span>${LBL[l]}`:'<span class="mut">无颜色</span>'):'';
  $('#mm').textContent=it&&it.mm?`⚠ RAW ${it.mm[0]|0}★ / HEIF ${it.mm[1]|0}★ 不一致，按 RAW`:'';
  pic.classList.remove('green','yellow','red');if(l)pic.classList.add(l);
  ov.innerHTML=it?(it.r?stars(it.r):'')+(it.mm?'<span class="mmb">⚠ RAW/HEIF 不一致</span>':''):'';
  $('#counts').innerHTML=`<span class="dot green"></span>${g} <span class="dot yellow"></span>${y} <span class="dot red"></span>${r} · ★ ${s}`+(mm?` · <span style="color:var(--o)">不一致 ${mm}</span>`:'');
  $('#fbtn').textContent='筛选：'+FILTERS[fi][1];
  $('#autob').textContent='自动跳：'+(auto?'开':'关');
  const w=$('#wbtn');w.textContent=pend?`全部写入（${pend}）`:'全部写入';w.classList.toggle('pend',pend>0);w.classList.toggle('pri',!pend);
  mark.style.display=it?'block':'none';if(it)mark.style.left=((cur+.5)/items.length*100)+'%';
  if(it&&it.err)showBadge('⚠ '+it.n+' 的 '+it.err,3000);
}

// 打标：先在界面上记下并立刻跳下一张，再按顺序发给本地工具保存；保存失败会提示并按已保存的状态刷新
const queue=[];let pumping=false;
function setMark(body,stay){
  if(cur<0)return;const i=cur,it=items[i];
  if('r' in body)it.r=body.r;if('l' in body)it.l=body.l;it.p=true;it.saving=(it.saving||0)+1;
  paintCell(i);
  if('r' in body)showBadge(body.r?'★'.repeat(body.r):'清除星级',500);
  else if(body.l)showBadgeHTML(`<span class="dot ${safeL(body.l)}"></span>${LBL[body.l]}`,500);
  else showBadge('清除颜色',500);
  queue.push({k:it.k,body,sid});pump();
  if(auto&&!stay){const n=step(i,1);if(n>=0){show(n);return}}
  if(!match(i)){let n=step(i,1);if(n<0)n=step(i,-1);show(n);return}
  updBar();
}
async function pump(){
  if(pumping)return;pumping=true;
  while(queue.length){
    const job=queue[0];let r=null,err='';
    try{r=await fetch('/api/set',{method:'POST',headers:{'Content-Type':'application/json','X-Cull3fr-Token':TOKEN},body:JSON.stringify(Object.assign({k:job.k,sid:job.sid},job.body))}).then(x=>x.json())}
    catch(e){err='本地工具没有响应'}
    if(r&&r.k){
      queue.shift();
      const idx=items.findIndex(x=>x.k===job.k);
      if(idx>=0){const left=(items[idx].saving||1)-1;if(left<=0){items[idx]=r}else{items[idx].saving=left}paintCell(idx);if(idx===cur)updBar()}
      continue;
    }
    queue.length=0;
    if(err){goneOffline();break}
    if(r&&r.stale){showBadge('文件夹已经切换，最后几下没有保存。',4000);try{await load(null,null)}catch(e){goneOffline()}break}
    try{await load();showBadge('⚠ '+((r&&r.text)||'保存失败')+'。最近几下没有记上，已按实际保存的状态刷新，请重新标。')}
    catch(e){goneOffline()}
    break;
  }
  pumping=false;
}

// 100%：用可以中途取消的请求，翻页时把没用的大图请求取消掉
let fullCtl=null,fullURL=null;
function cancelFull(){if(fullCtl){fullCtl.abort();fullCtl=null}}
async function enterZoom(fx,fy,pt){
  if(cur<0||!pic.naturalWidth)return;
  const it=items[cur],t=tok,dpr=window.devicePixelRatio||1;zoom={fx,fy};
  if(it.long===undefined){try{it.long=(await get('/api/dims?k='+enc(it.k))).long||0}catch(e){it.long=0}}
  if(t!==tok||!zoom)return;
  const nw=pic.naturalWidth,nh=pic.naturalHeight,s=it.long?it.long/Math.max(nw,nh):1;
  zimg.src=pic.src;zimg.style.width=(nw*s/dpr)+'px';zimg.style.height=(nh*s/dpr)+'px';
  zoomEl.style.display='block';stage.style.visibility='hidden';
  const px=pt?pt.x:zoomEl.clientWidth/2,py=pt?pt.y:zoomEl.clientHeight/2;
  zoomEl.scrollLeft=fx*zimg.offsetWidth-px;zoomEl.scrollTop=fy*zimg.offsetHeight-py;
  showBadge('100% 加载中…');
  cancelFull();const ctl=new AbortController();fullCtl=ctl;
  try{
    const res=await fetch(fURL(it),{signal:ctl.signal});if(!res.ok)throw new Error('HTTP '+res.status);
    const blob=await res.blob();if(t!==tok||!zoom)return;
    const u=URL.createObjectURL(blob);const full=new Image();
    full.onload=()=>{if(t!==tok||!zoom){URL.revokeObjectURL(u);return}hideBadge();
      const cx=(zoomEl.scrollLeft+zoomEl.clientWidth/2)/zimg.offsetWidth,cy=(zoomEl.scrollTop+zoomEl.clientHeight/2)/zimg.offsetHeight;
      zimg.src=u;zimg.style.width=(full.naturalWidth/dpr)+'px';zimg.style.height=(full.naturalHeight/dpr)+'px';
      zoomEl.scrollLeft=cx*zimg.offsetWidth-zoomEl.clientWidth/2;zoomEl.scrollTop=cy*zimg.offsetHeight-zoomEl.clientHeight/2;
      if(fullURL)URL.revokeObjectURL(fullURL);fullURL=u};
    full.src=u;
  }catch(e){if(e.name!=='AbortError'&&t===tok)showBadge('100% 加载失败',2000)}
  finally{if(fullCtl===ctl)fullCtl=null}
}
function exitZoom(){cancelFull();zoom=null;zoomEl.style.display='none';stage.style.visibility='visible';hideBadge();if(fullURL){zimg.src='';URL.revokeObjectURL(fullURL);fullURL=null}}
zoomEl.addEventListener('scroll',()=>{if(zoom&&zimg.offsetWidth)zoom={fx:(zoomEl.scrollLeft+zoomEl.clientWidth/2)/zimg.offsetWidth,fy:(zoomEl.scrollTop+zoomEl.clientHeight/2)/zimg.offsetHeight}});
function toggleZoom(){
  if(zoom){exitZoom();return}
  const r=pic.getBoundingClientRect();let fx=.5,fy=.5,pt=null;
  if(mouse&&mouse.x>=r.left&&mouse.x<=r.right&&mouse.y>=r.top&&mouse.y<=r.bottom){fx=(mouse.x-r.left)/r.width;fy=(mouse.y-r.top)/r.height;pt={x:mouse.x,y:mouse.y}}
  enterZoom(fx,fy,pt);
}
pic.addEventListener('click',e=>{const r=pic.getBoundingClientRect();enterZoom((e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height,{x:e.clientX,y:e.clientY})});
let drag=null;
zoomEl.addEventListener('mousedown',e=>{drag={x:e.clientX,y:e.clientY,sl:zoomEl.scrollLeft,st:zoomEl.scrollTop,moved:false};zoomEl.classList.add('drag');e.preventDefault()});
window.addEventListener('mousemove',e=>{mouse={x:e.clientX,y:e.clientY};if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>4)drag.moved=true;zoomEl.scrollLeft=drag.sl-dx;zoomEl.scrollTop=drag.st-dy});
window.addEventListener('mouseup',()=>{if(!drag)return;zoomEl.classList.remove('drag');if(!drag.moved)exitZoom();drag=null});

strip.addEventListener('click',e=>{keySeq++;const r=strip.getBoundingClientRect();let i=Math.min(items.length-1,Math.max(0,Math.floor((e.clientX-r.left)/r.width*items.length)));if(!match(i)){const n=step(i,1);i=n>=0?n:step(i,-1)}if(i>=0)show(i)});
function cycleFilter(d){fi=(fi+d+FILTERS.length)%FILTERS.length;paintStrip();if(cur>=0&&match(cur)){updBar();return}let n=step(cur,1);if(n<0)n=step(cur,-1);show(n)}
function toggleAuto(){auto=!auto;try{localStorage.setItem('cull3fr_auto',auto?'1':'0')}catch(e){}updBar();showBadge('自动下一张：'+(auto?'开':'关'),900)}

function openModal(id){document.querySelectorAll('.modal').forEach(m=>m.classList.remove('show'));$(id).classList.add('show')}
document.querySelectorAll('.modal .close').forEach(b=>b.onclick=()=>b.closest('.modal').classList.remove('show'));
async function waitQueue(){for(let n=0;n<100&&(queue.length||pumping);n++)await new Promise(r=>setTimeout(r,100))}
async function refreshPlan(){
  const p=await get('/api/plan?sid='+enc(sid));
  if(p.stale){$('#plan').textContent='文件夹已经切换，正在刷新…';await load();return refreshPlan()}
  $('#plan').textContent=p.text;
  $('#warn').textContent=p.phocus?'⚠ Phocus 正在运行。它之后保存时可能把刚写的值覆盖掉，建议先退出 Phocus 再写。':'';
  $('#wgo').style.display=p.phocus?'none':'';$('#wgo').disabled=!p.files;
  $('#wforce').style.display=p.phocus&&p.files?'':'none';
  $('#wundo').disabled=!p.canUndo;
}
async function openWrite(){openModal('#wdlg');$('#wres').textContent='';$('#plan').textContent='读取中…';await waitQueue();await refreshPlan()}
async function afterOp(r){$('#wres').textContent=(r&&r.text)||'';const it=cur>=0?items[cur]:null;await load(it&&it.k,it&&it.n);await refreshPlan()}
async function runOp(label,u,b){
  $('#wres').textContent=label;let r;
  try{await waitQueue();r=await api(u,b)}catch(e){$('#wres').textContent='⚠ 本地工具没有响应：'+e.message;return}
  if(r.stale){$('#wres').textContent='文件夹已经切换，已刷新，请再看一遍再点。';await load();await refreshPlan();return}
  if(r.needForce){$('#wres').textContent='Phocus 正在运行。';await refreshPlan();return}
  await afterOp(r);
}
$('#wgo').onclick=()=>runOp('写入中…','/api/write',{force:false});
$('#wforce').onclick=()=>runOp('写入中…','/api/write',{force:true});
$('#fgo').onclick=()=>runOp('移动中…','/api/folders',{});
$('#wundo').onclick=()=>runOp('撤销中…','/api/undo',{});
$('#wbtn').onclick=openWrite;$('#hbtn').onclick=()=>openModal('#hdlg');$('#fbtn').onclick=()=>cycleFilter(1);$('#autob').onclick=toggleAuto;
document.addEventListener('click',e=>{const b=e.target.closest('button');if(b)b.blur()});
async function openFolder(){
  showBadge('在弹出的窗口里选文件夹…');let r;
  try{await waitQueue();r=await api('/api/open',{})}catch(e){showBadge('⚠ 本地工具没有响应',3000);return}
  hideBadge();if(r.cancel)return;
  if(!r.ok){showBadge('⚠ '+(r.text||'打不开这个文件夹'),4000);return}
  fi=0;exitZoom();await load(null,null);showBadge('已打开 '+r.name,1500);
}
$('#fold').onclick=openFolder;
$('#retry').onclick=()=>location.reload();
$('#qbtn').onclick=()=>{const pend=items.filter(x=>x.p).length;$('#qwarn').textContent=pend?`还有 ${pend} 张的改动没写进 Phocus。不写也不会丢，下次打开还在。`:'';openModal('#qdlg')};
$('#qgo').onclick=async()=>{await waitQueue();quitted=true;try{await api('/api/quit',{})}catch(e){}openModal('#bye')};
$('#inst').onclick=async()=>{$('#instres').textContent='生成中…';try{const r=await api('/api/install',{});$('#instres').textContent=r.text}catch(e){$('#instres').textContent='生成失败'}};
async function goneOffline(){
  if(lost||quitted)return;
  try{const r=await get('/api/ping');if(!r.auth){location.reload();return}}catch(e){}  // 工具重启过：直接用新口令刷新
  if(lost)return;lost=true;queue.length=0;
  $('#lostmsg').textContent='本地工具已经退出，刚才这几下可能没有保存。已经保存的标记都在，重新打开 cull3fr 后会显示实际保存的状态。';
  openModal('#lost');
}
async function sync(){
  if(quitted)return;
  let r;
  try{r=await get('/api/ping')}catch(e){goneOffline();return}
  if(!r.auth){location.reload();return}   // 工具重启过：用新口令重新打开页面，显示实际保存的状态
  if(lost){lost=false;location.reload();return}
  if(r.sid&&sid&&r.sid!==sid){fi=0;exitZoom();await load(null,null);showBadge('已切换到 '+r.name,2000)}
}
setInterval(sync,20000);
// 切回这个窗口时，和另一个标签页 / 工具里的实际状态对齐；等数据期间按过键就不刷新，免得打断
window.addEventListener('focus',async()=>{
  if(quitted||lost||queue.length||pumping)return;
  const seq=keySeq;
  try{const r=await get('/api/ping');if(!r.auth){location.reload();return}if(r.sid!==sid)return sync();
    if(seq===keySeq&&!queue.length&&!pumping)await load(undefined,undefined,seq)}catch(e){goneOffline()}
});

const CODE_R={Digit0:0,Digit1:1,Digit2:2,Digit3:3,Digit4:4,Digit5:5,Numpad0:0,Numpad1:1,Numpad2:2,Numpad3:3,Numpad4:4,Numpad5:5};
const CODE_L={Digit6:'green',Digit7:'yellow',Digit8:'red',Digit9:null,Numpad6:'green',Numpad7:'yellow',Numpad8:'red',Numpad9:null,KeyG:'green',KeyY:'yellow',KeyR:'red'};
window.addEventListener('keydown',e=>{
  if(e.metaKey||e.ctrlKey||e.altKey||e.isComposing)return;
  const m=document.querySelector('.modal.show');
  if(m){if(e.key==='Escape'&&m.id!=='bye'&&m.id!=='lost')m.classList.remove('show');return}
  const c=e.code;keySeq++;
  if(lost)return;
  if(c in CODE_R||c in CODE_L){e.preventDefault();if(e.repeat)return;if(c in CODE_R)setMark({r:CODE_R[c]},e.shiftKey);else setMark({l:CODE_L[c]},e.shiftKey);return}
  if(e.key==='ArrowRight'||e.key==='ArrowDown'){const n=step(cur,1);if(n>=0)show(n);else showBadge('已经是最后一张',900)}
  else if(e.key==='ArrowLeft'||e.key==='ArrowUp'){const p=step(cur,-1);if(p>=0)show(p)}
  else if(c==='Space'){if(!e.repeat)toggleZoom()}
  else if(c==='KeyF')cycleFilter(e.shiftKey?-1:1);
  else if(c==='KeyA')toggleAuto();
  else if(c==='KeyE')openWrite();
  else if(c==='KeyO')openFolder();
  else if(e.key==='?'||c==='KeyH')openModal('#hdlg');
  else if(e.key==='Escape'){if(zoom)exitZoom()}
  else if(e.key==='Home'){const n=step(-1,1);if(n>=0)show(n)}
  else if(e.key==='End'){const n=step(items.length,-1);if(n>=0)show(n)}
  else return;
  e.preventDefault();
});
load().catch(e=>showBadge('⚠ 打不开：'+e.message));
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cull3fr/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", frame_block=False):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if frame_block:
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file(self, path):
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # 只接受本机地址 + 本次启动的随机口令：别的网站、DNS 重绑定都调不动这个服务
    def _host_ok(self):
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _token_ok(self, q=None):
        t = self.headers.get("X-Cull3fr-Token") or ""
        if not t and q is not None:
            t = (q.get("t") or [""])[0]
        return hmac.compare_digest(t.encode("utf-8"), self.server.token.encode("utf-8"))

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        S = self.server.sess
        if u.path == "/":
            self.server.last_seen = time.monotonic()
            page = PAGE.replace("__CULL3FR_TOKEN__", self.server.token)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", frame_block=True)
        if u.path == "/api/ping":
            if self._token_ok(q):
                self.server.last_seen = time.monotonic()
                return self._json({"app": "cull3fr", "auth": True, "sid": S.sid, "name": os.path.basename(S.root)})
            return self._json({"app": "cull3fr", "auth": False})
        if not self._token_ok(q):
            return self._send(403, b"forbidden", "text/plain")
        self.server.last_seen = time.monotonic()
        if u.path == "/api/items":
            return self._json(S.items_json())
        if u.path == "/api/plan":
            if (q.get("sid") or [""])[0] != S.sid:
                return self._json({"ok": False, "stale": True})
            return self._json(S.plan())
        it = S.by_key.get((q.get("k") or [""])[0])
        if u.path == "/api/dims":
            return self._json({"long": S.dims(it) if it else None})
        if u.path in ("/img/p", "/img/f"):
            if not it:
                return self._send(404, b"not found", "text/plain")
            try:
                return self._file(S.ensure(it, u.path[-1]))
            except StaleRequest:
                return self._send(409, b"stale", "text/plain")
            except Exception as ex:
                sys.stderr.write("预览失败 %s: %s\n" % (it["name"], ex))
                return self._send(500, str(ex).encode("utf-8"), "text/plain; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not (self._host_ok() and self._token_ok()):
            return self._send(403, b"forbidden", "text/plain")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, b"json only", "text/plain")
        self.server.last_seen = time.monotonic()
        S = self.server.sess
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._json({"ok": False, "text": "请求格式不对"}, 400)
        path = urlparse(self.path).path
        if path in ("/api/set", "/api/write", "/api/folders", "/api/undo") and body.get("sid") != S.sid:
            return self._json({"ok": False, "stale": True, "text": "文件夹已经切换，这一下没有保存。"})
        try:
            if path == "/api/set":
                try:
                    return self._json(S.set_mark(body.get("k"), body) or {"ok": False, "text": "找不到这张照片"})
                except (SaveError, ValueError) as ex:
                    return self._json({"ok": False, "text": str(ex)})
            if path == "/api/write":
                return self._json(S.write_all(bool(body.get("force"))))
            if path == "/api/folders":
                return self._json(S.move_to_folders())
            if path == "/api/undo":
                return self._json(S.undo())
            if path == "/api/open":
                return self._json(open_folder(self.server, body.get("path")))
            if path == "/api/quit":
                S.io_lock.acquire(timeout=300)  # 等正在进行的写入/移动做完
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/install":
                try:
                    app = install_app()
                    return self._json({"ok": True, "text": "已生成：%s\n以后用 Spotlight 搜「Cull3fr」打开，或者把它拖到 Dock。"
                                                          "\ncull3fr.py 请留在 %s，不要挪。"
                                                          % (app, os.path.dirname(os.path.abspath(__file__)))})
                except Exception as ex:
                    return self._json({"ok": False, "text": "生成失败：%s" % ex})
        except Exception as ex:
            sys.stderr.write("出错: %r\n" % ex)
            return self._json({"ok": False, "text": "出错：%s" % ex}, 500)
        self._send(404, b"not found", "text/plain")


def _as_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pick_folder(start=None):
    if sys.platform != "darwin":
        return None
    q = "choose folder with prompt " + _as_str("选择这次拍摄的文件夹")
    if start and os.path.isdir(start):
        q += " default location (POSIX file " + _as_str(start) + ")"
    r = subprocess.run(["osascript", "-e", "tell me to activate", "-e", "POSIX path of (" + q + ")"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def alert(msg):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", "display alert " + _as_str("Cull3fr") + " message " + _as_str(msg)],
                       capture_output=True)
    sys.stderr.write(msg + "\n")


def open_folder(server, path=None):
    cur = server.sess
    path = path or pick_folder(os.path.dirname(cur.root) if cur else None)
    if not path:
        return {"ok": False, "cancel": True}
    path = os.path.abspath(os.path.expanduser(str(path).strip()))
    if not os.path.isdir(path):
        return {"ok": False, "text": "不是文件夹：" + path}
    S = Session(path, server.preview_px)
    if not S.items:
        return {"ok": False, "text": "这个文件夹里没有 3FR / FFF / HEIF / JPG：" + path}
    with cur.io_lock:  # 等旧文件夹正在进行的写入做完再切换
        server.sess = S
    return {"ok": True, "name": os.path.basename(S.root), "sid": S.sid}


def install_app():
    """生成 ~/Applications/Cull3fr.app：双击 → 选文件夹 → 在后台启动本工具并打开浏览器。"""
    if sys.platform != "darwin":
        raise RuntimeError("只支持 macOS")
    app = os.path.expanduser("~/Applications/%s.app" % APP_NAME)
    log = os.path.expanduser("~/Library/Logs/cull3fr.log")
    os.makedirs(os.path.dirname(app), exist_ok=True)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    cmd = "PYTHONIOENCODING=utf-8 %s %s --launch " % (shlex.quote(sys.executable),
                                                       shlex.quote(os.path.abspath(__file__)))
    lines = [
        "on run",
        "try",
        "set f to POSIX path of (choose folder with prompt " + _as_str("选择这次拍摄的文件夹") + ")",
        "on error",
        "return",
        "end try",
        "do shell script " + _as_str(cmd) + " & quoted form of f & " + _as_str(" >> " + shlex.quote(log) + " 2>&1 &"),
        "end run",
    ]
    tmpd = tempfile.mkdtemp(prefix=".cull3fr-app-", dir=os.path.dirname(app))
    try:
        # osacompile 按 -o 的扩展名决定生成什么：必须以 .app 结尾才是 App，否则是普通脚本文件
        # （0.5 用的临时名 cull3fr.app.new 就因此生成了一个打不开的「App」）
        tmp_app = os.path.join(tmpd, APP_NAME + ".app")
        args = ["osacompile", "-o", tmp_app]
        for ln in lines:
            args += ["-e", ln]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "osacompile 失败")
        if not os.path.isfile(os.path.join(tmp_app, "Contents", "Info.plist")):
            raise RuntimeError("osacompile 没有生成 App")
        try:
            _set_app_icon(tmp_app)
        except Exception as e:
            sys.stderr.write("设置图标失败（不影响使用）：%s\n" % e)
        _run_quiet(["codesign", "--force", "--sign", "-", tmp_app])  # 改过资源后重新做本机签名
        for old in (app, os.path.join(os.path.dirname(app), "cull3fr.app")):  # 0.5 生成的是小写名字
            if os.path.isdir(old) and not os.path.islink(old):
                shutil.rmtree(old)
            elif os.path.lexists(old):
                os.remove(old)  # 0.5 生成的坏 App 是一个普通文件
        os.rename(tmp_app, app)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    os.utime(app, None)  # 让 Finder 重新读图标
    _run_quiet([LSREGISTER, "-f", app])
    _run_quiet(["open", "-R", app])
    return app


APP_NAME = "Cull3fr"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


def _run_quiet(args):
    try:
        return subprocess.run(args, capture_output=True, timeout=60).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _set_app_icon(app):
    res = os.path.join(app, "Contents", "Resources")
    with open(os.path.join(res, "applet.icns"), "wb") as f:
        f.write(icon_icns())
    car = os.path.join(res, "Assets.car")
    if os.path.exists(car):
        os.remove(car)  # 新版 macOS 生成的 applet 带 Assets.car，里面的默认图标会盖过 applet.icns
    p = os.path.join(app, "Contents", "Info.plist")
    with open(p, "rb") as f:
        info = plistlib.load(f)
    info.pop("CFBundleIconName", None)
    info["CFBundleIconFile"] = "applet"
    with open(p, "wb") as f:
        plistlib.dump(info, f)


# ---------- App 图标：灰绿色圆角方块 + 绿黄红三个圆点（按 1024 网格现场画，不带任何图片文件）

ICON_BG = (0x90, 0xa6, 0x9c)
ICON_DOTS = ((292, (0x34, 0xc7, 0x59)), (512, (0xff, 0xcc, 0x00)), (732, (0xff, 0x45, 0x3a)))
ICNS_TYPES = ((b"icp4", 16), (b"icp5", 32), (b"ic11", 32), (b"ic12", 64), (b"ic07", 128),
              (b"ic08", 256), (b"ic13", 256), (b"ic09", 512), (b"ic14", 512), (b"ic10", 1024))


def icon_png(n):
    import math
    import zlib
    s = n / 1024.0
    c, h, r = 512 * s, 412 * s, 185 * s  # 方块中心、半边长、圆角半径
    dr, dy = 75 * s, 512 * s             # 圆点半径、圆心 y
    dots = [(x * s, col) for x, col in ICON_DOTS]
    cache = {}

    def pixel(a, k, t):
        v = cache.get((a, k, t))
        if v is None:
            rgb = ICON_BG if k < 0 else tuple(round(b + (f - b) * t / 255) for b, f in zip(ICON_BG, dots[k][1]))
            v = cache[(a, k, t)] = bytes(rgb + (a,))
        return v

    raw = bytearray()
    for iy in range(n):
        y = iy + 0.5
        qy = abs(y - c) - (h - r)
        ady = abs(y - dy)
        line = []
        for ix in range(n):
            x = ix + 0.5
            qx = abs(x - c) - (h - r)
            d = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r  # 到圆角方块边缘的距离
            a = 0 if d >= 0.5 else 255 if d <= -0.5 else round((0.5 - d) * 255)
            if not a:
                line.append(b"\0\0\0\0")
                continue
            k, t = -1, 0
            if ady < dr + 0.5:
                for j, (cx, _) in enumerate(dots):
                    if abs(x - cx) < dr + 0.5:
                        e = math.hypot(x - cx, y - dy) - dr
                        if e < 0.5:
                            k, t = j, 255 if e <= -0.5 else round((0.5 - e) * 255)
                        break
            line.append(pixel(a, k, t))
        raw += b"\0" + b"".join(line)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def icon_icns():
    pngs, body = {}, b""
    for tag, n in ICNS_TYPES:
        if n not in pngs:
            pngs[n] = icon_png(n)
        body += tag + struct.pack(">I", 8 + len(pngs[n])) + pngs[n]
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


# ---------- 同一时间只运行一个（避免两个进程同时改同一份标记）

def _registry_path():
    return os.path.join(cache_base(), "server.json")


def acquire_instance_lock():
    os.makedirs(cache_base(), exist_ok=True)
    f = open(os.path.join(cache_base(), "instance.lock"), "a+")
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        return f
    except OSError:
        f.close()
        return None
    return f


def write_registry(port, token):
    p = _registry_path()
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    os.replace(tmp, p)


def clear_registry(token):
    try:
        with open(_registry_path()) as f:
            if json.load(f).get("token") == token:
                os.remove(_registry_path())
    except (OSError, ValueError):
        pass


def find_running(wait=0):
    import urllib.request
    end = time.time() + wait
    while True:
        try:
            with open(_registry_path()) as f:
                d = json.load(f)
            req = urllib.request.Request("http://127.0.0.1:%d/api/ping" % int(d["port"]),
                                         headers={"X-Cull3fr-Token": d["token"]})
            with urllib.request.urlopen(req, timeout=2) as r:
                if json.loads(r.read().decode("utf-8")).get("auth"):
                    return d
        except Exception:
            pass
        if time.time() >= end:
            return None
        time.sleep(0.5)


def _post_local(d, path, body):
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (int(d["port"]), path),
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "X-Cull3fr-Token": d["token"]})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="cull3fr：超轻量 Hasselblad 选片（评分 + 红黄绿，写入 Phocus .phos）")
    ap.add_argument("folder", nargs="?", help="照片文件夹（不填会弹窗选）")
    ap.add_argument("--undo", action="store_true", help="撤销最近一次写入或移动")
    ap.add_argument("--inspect", action="store_true", help="只打印读到的评分和颜色，不开界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--preview", type=int, default=3000, help="预览长边像素（没有内嵌预览时才用），默认 3000")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--launch", action="store_true", help=argparse.SUPPRESS)  # 由 App 启动
    ap.add_argument("--install-app", action="store_true", help="生成 ~/Applications/Cull3fr.app")
    a = ap.parse_args()

    if a.install_app:
        print("已生成：" + install_app())
        return
    if not SIPS:
        try:
            import PIL  # noqa: F401
        except ImportError:
            print("提示：不是 macOS（没有 sips），也没装 Pillow。相机内嵌的预览照样能看，"
                  "100% 放大和没有内嵌预览的文件需要 pip install pillow pillow-heif。")

    if a.inspect:
        folder = a.folder or pick_folder()
        if not folder or not os.path.isdir(folder):
            sys.exit("需要一个文件夹。")
        S = Session(folder, a.preview)
        print("共 %d 张：3FR %d，带 HEIF/JPG %d，已有 .phos %d，评分不一致 %d" % (
            len(S.items), sum(1 for it in S.items if S.is3fr(it)), sum(1 for it in S.items if it["comp"]),
            sum(1 for it in S.items if it["phos"]), sum(1 for it in S.items if S.mismatch(it))))
        for it in S.items:
            r, l = S.eff(it)
            print("%-14s %-12s RAW内=%s .phos=%s Approval=%s HEIF=%s → %d★ %s%s" % (
                it["name"], S.kind(it), it["in_r"], it["phos_r"], it["disk_a"] if it["phos"] else "-",
                it["heif_r"], r, l or "", "  ⚠不一致" if S.mismatch(it) else ""))
        return

    lock = acquire_instance_lock()
    if lock is None:
        # 已经有一个在运行：把文件夹交给它，打开浏览器就行
        d = find_running(wait=60)
        if not d:
            alert("cull3fr 好像已经在运行但没有响应。等一会儿再打开，或者重启电脑后再试。")
            sys.exit(1)
        if a.undo:
            sys.exit("cull3fr 正在运行：请在网页的「全部写入」里点「撤销」。")
        if a.folder:
            r = _post_local(d, "/api/open", {"path": a.folder})
            if not r.get("ok") and not r.get("cancel"):
                alert(r.get("text") or "打不开这个文件夹")
                return
        url = "http://127.0.0.1:%d/" % int(d["port"])
        if not a.no_browser:
            webbrowser.open(url)
        print("cull3fr 已经在运行：" + url)
        return

    threading.Thread(target=prune_cache, daemon=True).start()
    folder = a.folder or pick_folder()
    if not folder:
        sys.exit("没有选择文件夹。")
    folder = os.path.abspath(os.path.expanduser(folder.strip()))
    if not os.path.isdir(folder):
        alert("不是文件夹：" + folder)
        sys.exit(1)
    t0 = time.time()
    print("扫描 %s …" % folder)
    S = Session(folder, a.preview)
    if a.undo:
        print(S.undo()["text"])
        return
    if not S.items:
        alert("这个文件夹里没有 3FR / FFF / HEIF / JPG：" + folder)
        sys.exit(1)
    print("共 %d 张（%.1fs）" % (len(S.items), time.time() - t0))

    srv = None
    for port in [a.port] + list(range(a.port + 1, a.port + 20)) + [0]:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    port = srv.server_address[1]
    srv.daemon_threads = True
    srv.sess = S
    srv.preview_px = a.preview
    srv.token = secrets.token_urlsafe(24)
    srv.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    srv.last_seen = time.monotonic()
    write_registry(port, srv.token)
    url = "http://127.0.0.1:%d/" % port
    print("打开 %s  （在网页右下角点「退出」结束；标记会自动保存）" % url)
    if not a.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()

    def watchdog():
        # 网页关掉 60 分钟后自动退出（电脑睡眠的时间不算）；正在写入时等写完
        while True:
            time.sleep(30)
            if time.monotonic() - srv.last_seen > 3600:
                srv.sess.io_lock.acquire(timeout=300)
                srv.shutdown()
                return
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.sess.io_lock.acquire(timeout=300)
    finally:
        clear_registry(srv.token)
        lock.close()
    print("已退出。标记保存在 %s" % srv.sess.state_path)


if __name__ == "__main__":
    main()
