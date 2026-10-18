"""タイトル描画エンジン
重ね順: 座布団 → シャドウ → 押し出し → エッジ(外→内) → フェース
"""
import math
import os

from PIL import Image, ImageFilter
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QImage,
                           QLinearGradient, QPainter, QPainterPath,
                           QPainterPathStroker)

FMT = QImage.Format_ARGB32_Premultiplied


def qcolor(hex_, opacity=1.0):
    c = QColor(hex_)
    c.setAlphaF(max(0.0, min(1.0, c.alphaF() * opacity)))
    return c


def make_font(f):
    font = QFontDatabase.font(f["family"], f.get("style", ""), 12)
    if font.family() != f["family"]:
        font = QFont(f["family"])
    font.setPixelSize(max(1, int(round(f["size"]))))
    if f.get("tracking"):
        font.setLetterSpacing(QFont.AbsoluteSpacing, float(f["tracking"]))
    font.setKerning(True)
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


LINE_K_F = 1.0     # textplus_out.LINE_K と同じ
E_F_K = 1.0        # 前の行が大きいときに増える行間の係数（実測）

ANCHORS = {"tl": (0, 0), "tc": (0.5, 0), "tr": (1, 0),
           "ml": (0, 0.5), "mc": (0.5, 0.5), "mr": (1, 0.5),
           "bl": (0, 1), "bc": (0.5, 1), "br": (1, 1),
           "sl": (0, 1), "sc": (0.5, 1), "sr": (1, 1)}     # 旧ベースライン指定は下揃えとして読む


def anchor_of(t):
    """アンカー (ax, ay)。0=左/上, 0.5=中, 1=右/下。anchor は [ax, ay] か旧形式の "mc" など"""
    a = t.get("anchor", "mc")
    if isinstance(a, (list, tuple)) and len(a) == 2:
        return float(a[0]), float(a[1])
    return ANCHORS.get(a, (0.5, 0.5))


def _metrics(t):
    f = t["font"]
    font = make_font(f)
    fm = QFontMetricsF(font)
    lines = t["text"].split("\n") if t["text"] else [""]
    lh = fm.lineSpacing() * float(f.get("leading", 1.2))
    return font, fm, lines, lh


def anchor_point(t, a=None):
    """アンカー a=(ax, ay) の、回転・拡大前の位置(px)。a 省略時は今のアンカー"""
    ax, ay = a if a is not None else anchor_of(t)
    b = block_rect(t)
    return [b.left() + ax * b.width(), b.top() + ay * b.height()]


def char_styles(t):
    """文字ごとの指定（runs）を、文字の数と同じ長さのリストにする（改行も1文字）。指定なしは {}"""
    n = len(t.get("text") or "")
    out = [{} for _ in range(n)]
    for r in t.get("runs") or []:
        a = {k: v for k, v in r.items() if k not in ("s", "e")}
        for i in range(max(0, int(r.get("s", 0))), min(n, int(r.get("e", 0)))):
            d = dict(out[i])
            d.update(a)
            if isinstance(a.get("edges"), dict):
                ed = dict(out[i].get("edges") or {})
                ed.update(a["edges"])
                d["edges"] = ed
            out[i] = d
    return out


def _seg_key(a):
    import json as _j
    return _j.dumps(a, sort_keys=True)


def _char_font(t, a, _cache={}):
    """文字ごとの指定 a の文字のフォント（字間なし）。text_layout の fnt と同じ大きさの決め方"""
    f = t["font"]
    k = float((a or {}).get("size", 1))
    fs = (a or {}).get("font")
    ff = dict(f)
    ff["tracking"] = 0
    nk = 1.0
    if fs:
        ff["family"], ff["style"] = fs[0], (fs[1] if len(fs) > 1 else "")
        key = (tuple(fs), f["family"], f.get("style", ""))
        if key not in _cache:
            t0 = dict(ff); t0["size"] = 100.0
            m0 = QFontMetricsF(make_font(t0))
            b0 = dict(f); b0["size"] = 100.0; b0["tracking"] = 0
            mb = QFontMetricsF(make_font(b0))
            h0 = m0.ascent() + m0.descent()
            _cache[key] = ((mb.ascent() + mb.descent()) / h0) if h0 > 0 else 1.0
        nk = _cache[key]
    ff["size"] = float(f["size"]) * k * nk
    return make_font(ff)


def _ink(font, ch):
    """字面の左右の余白 -> (左の余白, 右の余白)。字面のない文字は None"""
    pp = QPainterPath()
    pp.addText(QPointF(0, 0), font, ch)
    r = pp.boundingRect()
    if r.width() <= 0:
        return None
    adv = QFontMetricsF(font).horizontalAdvance(ch)
    return r.left(), adv - r.right()


def char_kerns(t):
    """文字ごとの「後ろの間隔」(px) = 手のカーニング ＋ 縁の太さに合わせた自動の空き
    装飾（縁の太さ）が変わる境目だけ、隣どうしの字面の実際のすき間を測り、
    太い方の縁が隣の字面にかからないだけ空ける（font.edge_gap がオフなら手のカーニングだけ）"""
    text = t.get("text") or ""
    styles = char_styles(t)
    n = len(text)
    out = [float((styles[i] or {}).get("kern", 0) or 0) for i in range(n)]
    if not t["font"].get("edge_gap", True) or not any(styles):
        return out
    trk = float(t["font"].get("tracking", 0) or 0)

    def outer(i):
        a = styles[i] or {}
        return seg_edges(t, a)[1] * float(a.get("size", 1))

    ink_cache = {}

    def ink(i):
        a = styles[i] or {}
        key = (_seg_key(a), text[i])
        if key not in ink_cache:
            ink_cache[key] = _ink(_char_font(t, a), text[i])
        return ink_cache[key]

    for i in range(n - 1):
        if text[i] == "\n" or text[i + 1] == "\n":
            continue
        ei, ej = outer(i), outer(i + 1)
        if abs(ei - ej) <= 0.5:
            continue                      # 装飾が同じ所は空けない（縁がつながるのが普通）
        li, lj = ink(i), ink(i + 1)
        if li is None or lj is None:
            continue                      # 空白などは、もともと空いている
        ki = float((styles[i] or {}).get("size", 1))
        gap = li[1] + lj[0] + trk * ki + out[i]   # 今の字面どうしのすき間
        need = max(ei, ej) + 1.0                  # 太い縁が隣の字面にかからない幅
        if need > gap:
            out[i] += need - gap
    return out


def text_layout(t):
    """文字の配置。-> dict(path, block, line_paths, segs=[(path, 指定, 倍率, 行番号)])
    指定（runs）がなければ従来どおり1行を1つの形で描く"""
    f = t["font"]
    font, fm, lines, lh = _metrics(t)
    styles = char_styles(t)
    px, py = t["pos"]
    ax, ay = anchor_of(t)
    KK = char_kerns(t)
    has_size = any(abs(float(a.get("size", 1)) - 1) > 1e-6 or a.get("font") or a.get("bs")
                   for a in styles) or any(abs(k_) > 1e-6 for k_ in KK)

    # 行ごとに「同じ指定が続く区間」に分ける
    line_segs, idx = [], 0
    for line in lines:
        segs = []
        for ch_i, ch in enumerate(line):
            a = styles[idx + ch_i] if idx + ch_i < len(styles) else {}
            if segs and _seg_key(segs[-1][1]) == _seg_key(a):
                segs[-1][0] += ch
            else:
                segs.append([ch, a])
        line_segs.append(segs)
        idx += len(line) + 1

    _norm = {}

    def fnt(k, a=None):
        """倍率 k・文字ごとのフォント指定 a["font"]=[family, style] のフォント"""
        fs = (a or {}).get("font")
        if abs(k - 1) < 1e-6 and not fs:
            return font, fm
        ff = dict(f)
        nk = 1.0
        if fs:
            ff["family"], ff["style"] = fs[0], (fs[1] if len(fs) > 1 else "")
            key = tuple(fs)
            if key not in _norm:
                # Fusion はフォントごとの「上下の高さ(ascent+descent)」を文字サイズに合わせる
                t0 = dict(ff); t0["size"] = 100.0; t0["tracking"] = 0
                m0 = QFontMetricsF(make_font(t0))
                b0 = dict(f); b0["size"] = 100.0; b0["tracking"] = 0
                mb = QFontMetricsF(make_font(b0))
                h0 = m0.ascent() + m0.descent()
                _norm[key] = ((mb.ascent() + mb.descent()) / h0) if h0 > 0 else 1.0
            nk = _norm[key]
        ff["size"] = float(f["size"]) * k * nk
        ff["tracking"] = float(f.get("tracking", 0)) * k
        fk = make_font(ff)
        return fk, QFontMetricsF(fk)

    if not has_size:
        widths = [fm.horizontalAdvance(l) for l in lines]
        maxw = max(widths) if widths else 0.0
        total_h = lh * (len(lines) - 1) + fm.ascent() + fm.descent()
        baselines_rel = [fm.ascent() + i * lh for i in range(len(lines))]
    else:
        # 大きさの違う文字がある行: Fusion の Text+ と同じ考え方で行を送る（実測で確認）
        #   行の間隔 = 前の行の下(descent×前の行の最大倍率) + この行の上(ascent×この行の最大倍率)
        #            + 行間の余り(全体の行送り − ascent − descent)×前の行の最大倍率
        widths, kmaxs = [], []
        gi = 0
        for segs in line_segs:
            w, km = 0.0, 0.0
            for txt, a in segs:
                k = float(a.get("size", 1))
                fk, fmk = fnt(k, a)
                w += fmk.horizontalAdvance(txt) + sum(KK[gi:gi + len(txt)])
                gi += len(txt)
                km = max(km, k)
            gi += 1                                  # 改行
            widths.append(w)
            kmaxs.append(km if segs else 1.0)
        maxw = max(widths) if widths else 0.0
        asc, desc = fm.ascent(), fm.descent()
        extra = lh - (asc + desc)
        # 前の行が大きいときに増える行間は Fusion の LineSpacing から（全文字同じ大きさなら従来どおり）
        sp = max(1.0, float(f["size"]))
        ls_f = lh / (LINE_K_F * sp)                 # textplus_out.LINE_K と同じ換算
        e_f = (ls_f - 1.0) * E_F_K * sp            # 係数は Resolve での実測に合わせた
        baselines_rel, b = [], 0.0
        for i, kc in enumerate(kmaxs):
            if i == 0:
                b = asc * kc
            else:
                kp = kmaxs[i - 1]
                b += desc * kp + asc * kc + extra + e_f * (kp - 1.0)
            baselines_rel.append(b)
        total_h = (baselines_rel[-1] + desc * kmaxs[-1]) if kmaxs else 0.0

    left = px - ax * maxw
    top = py - ay * total_h
    cx = left + maxw / 2
    path = QPainterPath()
    line_paths, seg_out = [], []
    chars = [QRectF() for _ in range(len(t.get("text") or ""))]   # 文字ごとの枠（プレビューでの文字選択用）
    lstart = 0

    def put(ci, x0, x1, by_, fm_):
        if 0 <= ci < len(chars):
            chars[ci] = QRectF(x0, by_ - fm_.ascent(), x1 - x0, fm_.ascent() + fm_.descent())

    for i, line in enumerate(lines):
        w = widths[i]
        if f.get("align") == "left":
            x = left
        elif f.get("align") == "right":
            x = left + maxw - w
        else:
            x = cx - w / 2
        by = top + baselines_rel[i]
        lp = QPainterPath()
        if not has_size:
            lp.addText(QPointF(x, by), font, line)
            for m in range(len(line)):
                put(lstart + m, x + fm.horizontalAdvance(line[:m]), x + fm.horizontalAdvance(line[:m + 1]), by, fm)
            xe = x + fm.horizontalAdvance(line)
            put(lstart + len(line), xe, xe, by, fm)
            if len(line_segs[i]) > 1 or (line_segs[i] and line_segs[i][0][1]):
                done = ""
                for txt, a in line_segs[i]:
                    sp = QPainterPath()
                    sp.addText(QPointF(x + fm.horizontalAdvance(done), by), font, txt)
                    seg_out.append((sp, a, 1.0, i))
                    done += txt
            else:
                seg_out.append((lp, {}, 1.0, i))
        else:
            xx = x
            m0 = 0
            for txt, a in line_segs[i]:
                k = float(a.get("size", 1))
                fk, fmk = fnt(k, a)
                g0 = lstart + m0
                ks = KK[g0:g0 + len(txt)]                     # 各文字の後ろの詰め/空け(px)
                cum = [0.0]
                for kv in ks:
                    cum.append(cum[-1] + kv)
                bys = by - float(a.get("bs", 0) or 0)         # ベースラインのずらし(px、上が＋)
                sp = QPainterPath()
                if any(abs(kv) > 1e-6 for kv in ks[:-1]):
                    for m, ch in enumerate(txt):            # 1文字ずつ置く
                        sp.addText(QPointF(xx + fmk.horizontalAdvance(txt[:m]) + cum[m], bys), fk, ch)
                else:
                    sp.addText(QPointF(xx, bys), fk, txt)
                seg_out.append((sp, a, k, i))
                lp.addPath(sp)
                for m in range(len(txt)):
                    put(lstart + m0 + m, xx + fmk.horizontalAdvance(txt[:m]) + cum[m],
                        xx + fmk.horizontalAdvance(txt[:m + 1]) + cum[m], bys, fmk)
                m0 += len(txt)
                xx += fmk.horizontalAdvance(txt) + cum[-1]
            put(lstart + len(line), xx, xx, by, fm)
        line_paths.append(lp)
        path.addPath(lp)
        lstart += len(line) + 1
    return {"path": path, "block": QRectF(left, top, maxw, total_h), "line_paths": line_paths, "segs": seg_out,
            "chars": chars, "line_starts": _line_starts(lines)}


def _line_starts(lines):
    out, n = [], 0
    for ln in lines:
        out.append(n)
        n += len(ln) + 1
    return out


def text_path(t):
    """文字のアウトライン(QPainterPath)と文字ブロックの矩形を返す。pos は文字ブロックのアンカー位置"""
    L = text_layout(t)
    return L["path"], L["block"], L["line_paths"]


def outline(path, width):
    # 字形の輪郭は重なった線でできていることがあり、そのまま縁を付けると角に段差ができる。
    # 重なりを一つにまとめてから縁を作る（Text+ 側は Clean Intersections で同じ処理）
    path = path.simplified()
    s = QPainterPathStroker()
    s.setWidth(width * 2)
    s.setJoinStyle(Qt.RoundJoin)
    s.setCapStyle(Qt.RoundCap)
    return s.createStroke(path)


def _new(rect):
    img = QImage(max(1, rect.width()), max(1, rect.height()), FMT)
    img.fill(0)
    return img


def _painter(img):
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    return p


def blur_tint(img, radius, color):
    """img のアルファだけを使い、ぼかして単色に塗る"""
    a = img.convertToFormat(QImage.Format_Alpha8)
    w, h, bpl = a.width(), a.height(), a.bytesPerLine()
    buf = bytes(a.constBits())[: bpl * h]
    L = Image.frombuffer("L", (w, h), buf, "raw", "L", bpl, 1)
    if radius > 0:
        L = L.filter(ImageFilter.GaussianBlur(radius))
    c = QColor(color)
    rgba = Image.new("RGBA", (w, h), (c.red(), c.green(), c.blue(), 0))
    rgba.putalpha(L)
    data = rgba.tobytes()
    return QImage(data, w, h, 4 * w, QImage.Format_RGBA8888).copy()


def make_gradient(rect, fc):
    """矩形と角度から線形グラデーションを作る（角度90=上→下, 0=左→右）"""
    a = math.radians(float(fc.get("angle", 90)))
    dx, dy = math.cos(a), math.sin(a)
    half = (abs(dx) * rect.width() + abs(dy) * rect.height()) / 2
    c = rect.center()
    g = QLinearGradient(QPointF(c.x() - dx * half, c.y() - dy * half),
                        QPointF(c.x() + dx * half, c.y() + dy * half))
    stops = fc.get("stops") or [[0, fc.get("color", "#ffffff"), 1], [1, fc.get("color2", "#ffd200"), 1]]
    op = float(fc.get("opacity", 1.0))
    for st in sorted(stops, key=lambda x: float(x[0])):
        a_ = float(st[2]) if len(st) > 2 else 1.0
        g.setColorAt(max(0.0, min(1.0, float(st[0]))), qcolor(st[1], a_ * op))
    return g


FACE_FITS = [("切り抜いて全体を覆う", "cover"), ("画像全体を入れる", "contain"), ("文字に合わせて引き伸ばす", "stretch")]


def face_img_on(t):
    fc = t.get("face") or {}
    im = fc.get("img") or {}
    return bool(fc.get("enabled") and fc.get("img_on") and im.get("file") and os.path.isfile(im["file"]))


def face_img_dest(rect, t):
    """画像を置く矩形（文字の範囲 rect に、当て方と大きさで合わせる）"""
    im = t["face"]["img"]
    iw, ih = max(1.0, float(im.get("w") or 1)), max(1.0, float(im.get("h") or 1))
    fit = im.get("fit", "cover")
    k = float(im.get("scale", 100) or 100) / 100.0
    if fit == "stretch":
        w, h = rect.width() * k, rect.height() * k
    else:
        s_ = max(rect.width() / iw, rect.height() / ih) if fit == "cover" else min(rect.width() / iw, rect.height() / ih)
        w, h = iw * s_ * k, ih * s_ * k
    c = rect.center()
    return QRectF(c.x() - w / 2, c.y() - h / 2, w, h)


def face_img_parts(t, L=None):
    """画像で塗る範囲 -> [(行番号 or None, 文字の範囲の矩形, 画像を置く矩形)]"""
    L = L or text_layout(t)
    if t["face"]["img"].get("span", "block") == "line" and L["line_paths"]:
        out = []
        for li, lp_ in enumerate(L["line_paths"]):
            r = lp_.boundingRect()
            if r.width() > 0 and r.height() > 0:
                out.append((li, r, face_img_dest(r, t)))
        return out
    r = L["path"].boundingRect()
    return [(None, r, face_img_dest(r, t))]


EMBOSS_K = 1.0      # エンボスの強さの換算（Fusion の式と共通）


def emboss_image(path, r2, emb):
    """エンボスの明暗（白と黒、面の中だけ）。式: s = 強さ×幅×2 × (−∇h・L)、h = ぼかした面"""
    from PIL import ImageChops, ImageMath
    m = _new(r2)
    mp = _painter(m)
    mp.translate(-r2.x(), -r2.y())
    mp.fillPath(path, Qt.white)
    mp.end()
    w, h = m.width(), m.height()
    a8 = m.convertToFormat(QImage.Format_Alpha8)
    face = Image.frombuffer("L", (w, h), bytes(a8.constBits())[: a8.bytesPerLine() * h], "raw", "L",
                            a8.bytesPerLine(), 1)
    blur = float(emb.get("blur", 0))
    hgt = face.filter(ImageFilter.GaussianBlur(blur)) if blur > 0 else face
    F = hgt.convert("F")
    ang = math.radians(float(emb.get("angle", 225)))
    lx, ly = math.cos(ang), math.sin(ang)
    k = float(emb.get("depth", 1)) * max(1.0, blur) * 2 * EMBOSS_K / 255.0 / 2.0
    xp, xm = ImageChops.offset(F, -1, 0), ImageChops.offset(F, 1, 0)
    yp, ym = ImageChops.offset(F, 0, -1), ImageChops.offset(F, 0, 1)
    S = ImageMath.lambda_eval(lambda e: -((e["xp"] - e["xm"]) * e["lx"] + (e["yp"] - e["ym"]) * e["ly"]) * e["k"],
                              xp=xp, xm=xm, yp=yp, ym=ym, lx=lx, ly=ly, k=k)
    # 明るい所（白）と暗い所（黒）のアルファ
    hiA = ImageMath.lambda_eval(lambda e: e["convert"](e["min"](e["max"](e["S"] * 255.0, 0.0), 255.0), "L"), S=S)
    loA = ImageMath.lambda_eval(lambda e: e["convert"](e["min"](e["max"](e["S"] * -255.0, 0.0), 255.0), "L"), S=S)
    hiA = ImageChops.multiply(hiA, face)
    loA = ImageChops.multiply(loA, face)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    black.putalpha(loA)
    white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    white.putalpha(hiA)
    out = Image.alpha_composite(Image.alpha_composite(out, black), white)
    data = out.tobytes()
    return QImage(data, w, h, 4 * w, QImage.Format_RGBA8888).copy()


def extrude_bands(ex):
    """押し出しのグラデーションの帯 -> [(番号, 奥行き px, 色)]  番号0が手前（Text+ も同じ帯で作る）"""
    depth = max(1, int(round(float(ex.get("depth", 1)))))
    K = max(1, min(depth, 16))
    c1, c2 = QColor(ex.get("color", "#303030")), QColor(ex.get("color2", "#000000"))
    out = []
    for j in range(K):
        n = max(1, int(round(depth * (j + 1) / K)))
        u = j / (K - 1) if K > 1 else 0.0
        col = QColor(round(c1.red() + (c2.red() - c1.red()) * u), round(c1.green() + (c2.green() - c1.green()) * u),
                     round(c1.blue() + (c2.blue() - c1.blue()) * u))
        out.append((j, n, col))
    return out


def edge_grad_rect(rect, e):
    """縁のグラデーションをかける範囲（Fusion に合わせる。実測で調整）"""
    return rect


def edge_grad_on(e):
    return bool(e.get("gradient")) and len(e.get("stops") or []) >= 2


def _fill_edge(lp, l_rect, shape, base, e, brush, soft, opacity):
    """縁を塗る（brush は色かグラデーション）。ソフトはぼかしたマスクに塗る"""
    if soft > 0:
        m = _new(l_rect)
        mp = _painter(m)
        mp.translate(-l_rect.x(), -l_rect.y())
        mp.fillPath(shape, Qt.white)
        mp.fillPath(base, Qt.white)
        mp.end()
        img = blur_tint(m, soft, "#ffffff")
        ip = _painter(img)
        ip.setCompositionMode(QPainter.CompositionMode_SourceIn)
        ip.translate(-l_rect.x(), -l_rect.y())
        ip.fillRect(QRectF(l_rect), brush)
        ip.end()
        lp.save()
        lp.setOpacity(opacity)
        lp.drawImage(l_rect.topLeft(), img)
        lp.restore()
    else:
        lp.save()
        lp.setOpacity(opacity)
        lp.fillPath(shape, brush)
        lp.fillPath(base, brush)
        lp.restore()


def box_rect(t, block):
    """座布団の矩形。fit=文字に追従 / free=独立した位置とサイズ"""
    bx = t["box"]
    if bx.get("mode") == "free":
        cx, cy, w, h = bx["rect"]
        return QRectF(cx - w / 2, cy - h / 2, w, h)
    return block.adjusted(-bx["padx"], -bx["pady"], bx["padx"], bx["pady"])


def _apply_xf(t, canvas, info):
    """テロップ全体を pos を中心に回転・拡大する（回転は反時計回りが＋）"""
    xf = t.get("xf") or {}
    rot = float(xf.get("rot", 0) or 0)
    sx = float(xf.get("sx", 100) or 100) / 100.0
    sy = float(xf.get("sy", 100) or 100) / 100.0
    if abs(rot) < 1e-6 and abs(sx - 1) < 1e-6 and abs(sy - 1) < 1e-6:
        return canvas, info
    from PySide6.QtGui import QTransform
    cx, cy = t["pos"]
    tr = QTransform()
    tr.translate(cx, cy)
    tr.rotate(-rot)
    tr.scale(sx, sy)
    tr.translate(-cx, -cy)
    out = QImage(canvas.width(), canvas.height(), FMT)
    out.fill(0)
    p = _painter(out)
    p.setTransform(tr)
    p.drawImage(0, 0, canvas)
    p.end()
    info = dict(info)
    for k in ("text", "block", "box"):
        if info.get(k) is not None:
            info[k] = tr.mapRect(info[k])
    return out, info


def render(t, parts=("box", "text")):
    if is_image(t):                       # 画像は回転・拡大まで済んでいる
        return _render_image(t)
    canvas, info = _render(t, parts)
    raw = QRectF(info["text"])
    if info.get("box") is not None:
        raw = raw.united(info["box"])
    info["raw"] = raw                     # 回転・拡大する前の範囲（バウンディングボックス用）
    return _apply_xf(t, canvas, info)


# ---- 画像のテロップ ----
_IMG_CACHE = {}


def is_image(t):
    return (t or {}).get("kind") == "image"


def load_image(path):
    """画像ファイルを読む（読めなければ None）。同じファイルは使い回す"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)
    if key not in _IMG_CACHE:
        img = QImage(path)
        if img.isNull():
            return None
        _IMG_CACHE.clear() if len(_IMG_CACHE) > 40 else None
        _IMG_CACHE[key] = img.convertToFormat(FMT)
    return _IMG_CACHE[key]


def image_rect(t):
    """画像の矩形（回転・拡大前。拡大は xf で別にかかる）"""
    im = t.get("image") or {}
    w, h = float(im.get("w", 100) or 100), float(im.get("h", 100) or 100)
    ax, ay = anchor_of(t)
    px, py = t["pos"]
    return QRectF(px - ax * w, py - ay * h, w, h)


def image_xf(t):
    """画像の回転・拡大（pos を中心に）"""
    from PySide6.QtGui import QTransform
    xf = t.get("xf") or {}
    cx, cy = t["pos"]
    tr = QTransform()
    tr.translate(cx, cy)
    tr.rotate(-float(xf.get("rot", 0) or 0))
    tr.scale(float(xf.get("sx", 100) or 100) / 100.0, float(xf.get("sy", 100) or 100) / 100.0)
    tr.translate(-cx, -cy)
    return tr


def _render_image(t):
    W, H = int(t["canvas"][0]), int(t["canvas"][1])
    canvas = QImage(W, H, FMT)
    canvas.fill(0)
    r = image_rect(t)
    im = t.get("image") or {}
    src = load_image(im.get("file", ""))
    # 回転・拡大はここで直接かける（原寸で画面に描いてから縮めると、画面の外にはみ出た部分が欠けるため）
    tr = image_xf(t)
    p = _painter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setTransform(tr)
    p.setOpacity(max(0.0, min(1.0, float(im.get("opacity", 1.0)))))
    if src is not None:
        p.drawImage(r, src)
    else:                                   # ファイルが見つからないとき
        p.setOpacity(1.0)
        p.setPen(QColor(255, 80, 80))
        p.drawRect(r)
        p.drawLine(r.topLeft(), r.bottomRight())
        p.drawLine(r.topRight(), r.bottomLeft())
    p.end()
    m = tr.mapRect(r)
    return canvas, {"text": m, "block": QRectF(m), "box": None, "raw": QRectF(r)}


def block_rect(t):
    """回転・拡大前の文字ブロック矩形（画像のテロップは画像の矩形）"""
    if is_image(t):
        return image_rect(t)
    if t["text"].strip():
        return text_path(t)[1]
    return QRectF(t["pos"][0] - 50, t["pos"][1] - 20, 100, 40)


def set_block_center(t, cx, cy):
    """文字ブロックの中心が (cx, cy) になるように pos を動かす（アンカーはそのまま）"""
    b = block_rect(t)
    t["pos"][0] += cx - b.center().x()
    t["pos"][1] += cy - b.center().y()


def seg_edges(t, a):
    """区間 a（文字ごとの指定）で使う縁 -> ([(縁番号, 縁の設定, 色, 外側までの累計の太さ)], 累計)"""
    ew = (a or {}).get("ew") or {}
    ec = (a or {}).get("edges") or {}
    es = (a or {}).get("es") or {}
    eo = (a or {}).get("eo") or {}
    out, cum = [], 0.0
    for i, e in enumerate(t["edges"]):
        if str(i) in ew:
            w = float(ew[str(i)])
        else:
            w = float(e.get("width", 0)) if e.get("enabled") else 0.0
        if w <= 0:
            continue
        cum += w
        if str(i) in es or str(i) in eo:
            e = dict(e)
            if str(i) in es:
                e["soft"] = float(es[str(i)])
            if str(i) in eo:
                e["opacity"] = float(eo[str(i)])
        out.append((i, e, ec.get(str(i)) or e["color"], cum))
    return out, cum


def used_edges(t):
    """テロップ全体か、どれかの文字で使っている縁の番号"""
    used = {i for i, e in enumerate(t["edges"]) if e.get("enabled") and e.get("width", 0) > 0}
    for r in t.get("runs") or []:
        for k, w in (r.get("ew") or {}).items():
            if float(w) > 0 and int(k) < len(t["edges"]):
                used.add(int(k))
    return sorted(used)


def seg_shadow(t, a):
    """区間 a の影 -> (出すか, 色, ぼかし, 不透明度, 距離, 方向)"""
    sh = t["shadow"]
    a = a or {}
    on = bool(a.get("sh_on", sh.get("enabled")))
    op = float(a.get("sh_op", sh.get("opacity", 0)))
    return (on and op > 0, a.get("sh_color", sh["color"]), float(a.get("sh_blur", sh.get("blur", 0))), op,
            float(a.get("sh_dist", sh.get("distance", 0))), float(a.get("sh_angle", sh.get("angle", 45))))


def _render(t, parts=("box", "text")):
    """キャンバスサイズの透過画像と、当たり判定用の情報を返す
    info = {"text": 文字(エッジ込み)の矩形, "block": 文字ブロック矩形, "box": 座布団矩形 or None}
    """
    W, H = int(t["canvas"][0]), int(t["canvas"][1])
    canvas = QImage(W, H, FMT)
    canvas.fill(0)
    cp = _painter(canvas)
    info = {"text": QRectF(), "block": QRectF(), "box": None}

    has_text = bool(t["text"].strip())
    if has_text:
        L = text_layout(t)
        path, block, line_paths, segs = L["path"], L["block"], L["line_paths"], L["segs"]
    else:
        path, block, line_paths, segs = QPainterPath(), QRectF(t["pos"][0] - 50, t["pos"][1] - 20, 100, 40), [], []
    styled = any(a or abs(k - 1) > 1e-6 for _, a, k, _ in segs)
    info["block"] = block
    info["text"] = QRectF(block)

    # 座布団
    bx = t["box"]
    if bx.get("enabled"):
        r = box_rect(t, block)
        if "box" in parts:
            cp.setPen(Qt.NoPen)
            cp.setBrush(qcolor(bx["color"], bx["opacity"]))
            cp.drawRoundedRect(r, bx["radius"], bx["radius"])
        info["box"] = r

    if not has_text or "text" not in parts:
        cp.end()
        return canvas, info

    edges = [e for e in t["edges"] if e.get("enabled") and e.get("width", 0) > 0]
    cums, cum = [], 0.0
    for e in edges:
        cum += float(e["width"])
        cums.append(cum)
    kmax = max([k for _, _, k, _ in segs] + [1.0])
    if styled:                                    # 文字ごとの縁（太さ・大きさ）
        sinfo = [(sp, a, k, li) + seg_edges(t, a) for sp, a, k, li in segs]
        outer = QPainterPath()
        cpad = 0.0
        for sp, a, k, li, se, sc in sinfo:
            if sc > 0:
                outer.addPath(outline(sp, sc * k))
                cpad = max(cpad, sc * k)
        if cpad <= 0:
            outer = None
        soft_pad = max([float(x[1].get("soft", 0)) * k for sp, a, k, li, se, sc in sinfo for x in se]
                       + [0.0]) * 3
    else:
        sinfo = []
        outer = outline(path, cum) if cum > 0 else None
        cpad = cum
        soft_pad = max([float(e.get("soft", 0)) for e in edges] + [0.0]) * 3

    # シルエット(文字+全エッジ)のマスク
    sil_rect = path.boundingRect().adjusted(-cpad - 3, -cpad - 3, cpad + 3, cpad + 3).toAlignedRect()
    sil = _new(sil_rect)
    p = _painter(sil)
    p.translate(-sil_rect.x(), -sil_rect.y())
    p.fillPath(path, Qt.white)
    if outer is not None:
        p.fillPath(outer, Qt.white)
    p.end()
    info["text"] = QRectF(sil_rect)

    # 押し出し（単色）
    ex = t["extrude"]
    ext_img, ext_rect = None, None
    if ex.get("enabled") and ex.get("depth", 0) > 0:
        depth = int(round(float(ex["depth"])))
        a = math.radians(ex["angle"])
        dx, dy = math.cos(a), math.sin(a)
        ext_rect = sil_rect.united(sil_rect.translated(round(dx * depth), round(dy * depth))).adjusted(-1, -1, 1, 1)
        ext_img = _new(ext_rect)
        ep = _painter(ext_img)
        ox, oy = sil_rect.x() - ext_rect.x(), sil_rect.y() - ext_rect.y()
        if ex.get("grad"):                        # 手前→奥のグラデーション（奥の帯から順に重ねる）
            for j, n, col in reversed(extrude_bands(ex)):
                band = _new(ext_rect)
                bp = _painter(band)
                for i in range(n, 0, -1):
                    bp.drawImage(QPointF(ox + dx * i, oy + dy * i), sil)
                bp.setCompositionMode(QPainter.CompositionMode_SourceIn)
                bp.fillRect(band.rect(), col)
                bp.end()
                ep.drawImage(0, 0, band)
            ep.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            ep.fillRect(ext_img.rect(), QColor(0, 0, 0, int(round(255 * float(ex.get("opacity", 1.0))))))
        else:
            for i in range(depth, 0, -1):
                ep.drawImage(QPointF(ox + dx * i, oy + dy * i), sil)
            ep.setCompositionMode(QPainter.CompositionMode_SourceIn)
            ep.fillRect(ext_img.rect(), qcolor(ex["color"], ex.get("opacity", 1.0)))
        ep.end()

    # シャドウ（押し出しも含めた形で落とす）
    sh = t["shadow"]
    per_sh = styled and any(any(str(kk).startswith("sh_") for kk in a) for _, a, _, _ in segs)
    if per_sh:                                    # 文字ごとの影（出す/色/ぼかし/不透明度）
        groups = {}
        for sp, a, k, li, se, sc in sinfo:
            on, col, blur, op, dist, angd = seg_shadow(t, a)
            if on:
                groups.setdefault((col, blur, op, dist, angd), []).append((sp, k, sc))
        for (col, blur, op, dist, angd), items in groups.items():
            ang = math.radians(angd)
            pad = int(math.ceil(blur * 3)) + 2
            base_rect = ext_rect if ext_rect is not None else sil_rect
            m_rect = base_rect.adjusted(-pad, -pad, pad, pad)
            m = _new(m_rect)
            mp = _painter(m)
            mp.translate(-m_rect.x(), -m_rect.y())
            gp = QPainterPath()
            for sp, k, sc in items:
                gp.addPath(sp)
                if sc > 0:
                    gp.addPath(outline(sp, sc * k))
            mp.fillPath(gp, Qt.white)
            if ext_img is not None:
                depth = int(round(float(ex["depth"])))
                ea = math.radians(ex["angle"])
                for i in range(1, depth + 1):
                    mp.fillPath(gp.translated(math.cos(ea) * i, math.sin(ea) * i), Qt.white)
            mp.end()
            shadow = blur_tint(m, blur, col)
            cp.save()
            cp.setOpacity(op)
            cp.drawImage(QPointF(m_rect.x() + math.cos(ang) * dist, m_rect.y() + math.sin(ang) * dist), shadow)
            cp.restore()
    elif sh.get("enabled") and sh.get("opacity", 0) > 0:
        blur = float(sh.get("blur", 0))
        pad = int(math.ceil(blur * 3)) + 2
        base_rect = ext_rect if ext_rect is not None else sil_rect
        m_rect = base_rect.adjusted(-pad, -pad, pad, pad)
        m = _new(m_rect)
        mp = _painter(m)
        mp.drawImage(sil_rect.topLeft() - m_rect.topLeft(), sil)
        if ext_img is not None:
            mp.drawImage(ext_rect.topLeft() - m_rect.topLeft(), ext_img)
        mp.end()
        shadow = blur_tint(m, blur, sh["color"])
        a = math.radians(sh["angle"])
        d = float(sh["distance"])
        cp.save()
        cp.setOpacity(float(sh["opacity"]))
        cp.drawImage(QPointF(m_rect.x() + math.cos(a) * d, m_rect.y() + math.sin(a) * d), shadow)
        cp.restore()

    if ext_img is not None:
        cp.drawImage(ext_rect.topLeft(), ext_img)

    # 文字レイヤー（エッジ外→内、ソフト対応 → 中抜き → フェース）
    pad = int(math.ceil(soft_pad)) + 2
    l_rect = sil_rect.adjusted(-pad, -pad, pad, pad)
    layer = _new(l_rect)
    lp = _painter(layer)
    lp.translate(-l_rect.x(), -l_rect.y())
    if styled:
        # 文字ごとの色・太さ・大きさ: 外側の縁から順に、区間ごとに描く
        for ei in reversed(range(len(t["edges"]))):
            for sp, a, k, li, se, sc in sinfo:
                hit = [x for x in se if x[0] == ei]
                if not hit:
                    continue
                _, e, col_hex, c = hit[0]
                shp = outline(sp, c * k)
                soft = float(e.get("soft", 0)) * k
                ge = t["edges"][ei]
                if edge_grad_on(ge) and not (a.get("edges") or {}).get(str(ei)):
                    ref = line_paths[li] if ge.get("span", "line") == "line" and li < len(line_paths) else path
                    brush = make_gradient(edge_grad_rect(ref.boundingRect(), ge), dict(ge, opacity=1.0))
                else:
                    brush = qcolor(col_hex)
                _fill_edge(lp, l_rect, shp, sp, e, brush, soft, float(e.get("opacity", 1.0)))
    for (e, c) in ([] if styled else reversed(list(zip(edges, cums)))):
        soft = float(e.get("soft", 0))
        op = float(e.get("opacity", 1.0))
        if edge_grad_on(e):                       # 縁のグラデーション（行ごと／全体）
            parts_ = line_paths if e.get("span", "line") == "line" and line_paths else [path]
            for part in parts_:
                g = make_gradient(edge_grad_rect(part.boundingRect(), e), dict(e, opacity=1.0))
                _fill_edge(lp, l_rect, outline(part, c), part, e, g, soft, op)
        else:
            _fill_edge(lp, l_rect, outline(path, c), path, e, qcolor(e["color"]), soft, op)
    lp.setCompositionMode(QPainter.CompositionMode_DestinationOut)
    lp.fillPath(path, Qt.black)
    lp.setCompositionMode(QPainter.CompositionMode_SourceOver)
    fc = t["face"]
    if face_img_on(t):                            # 画像で塗る（文字ごとに色を指定した文字はその色）
        src = load_image(fc["img"]["file"])
        parts_ = face_img_parts(t, L)
        own = QPainterPath()
        if styled:
            for sp, a, k, li in segs:
                if a.get("face"):
                    lp.fillPath(sp, qcolor(a["face"], fc.get("opacity", 1.0)))
                    own.addPath(sp)
        for li, r, dest in parts_:
            clip = line_paths[li] if li is not None else path
            if not own.isEmpty():
                clip = clip.subtracted(own)
            lp.fillPath(clip, qcolor(fc["color"], fc.get("opacity", 1.0)))   # 画像の外（当て方が「全体を入れる」など）は面の色
            lp.save()
            lp.setClipPath(clip)
            lp.setOpacity(float(fc.get("opacity", 1.0)))
            lp.setRenderHint(QPainter.SmoothPixmapTransform, True)
            if src is not None:
                lp.drawImage(dest, src)
            lp.restore()
    elif fc.get("enabled") and styled:
        for sp, a, k, li in segs:
            if a.get("face"):
                lp.fillPath(sp, qcolor(a["face"], fc.get("opacity", 1.0)))
            elif fc.get("gradient"):
                ref = line_paths[li] if fc.get("span", "line") == "line" and li < len(line_paths) else path
                lp.fillPath(sp, make_gradient(ref.boundingRect(), fc))
            else:
                lp.fillPath(sp, qcolor(fc["color"], fc.get("opacity", 1.0)))
    elif fc.get("enabled"):
        if fc.get("gradient"):
            parts = line_paths if fc.get("span", "line") == "line" else [path]
            for part in parts:
                lp.fillPath(part, make_gradient(part.boundingRect(), fc))
        else:
            lp.fillPath(path, qcolor(fc["color"], fc.get("opacity", 1.0)))
    lp.end()
    cp.drawImage(l_rect.topLeft(), layer)
    # 面のエンボス（面の形をぼかして高さにし、光の向きで明るく／暗くする。Fusion も同じ式）
    emb = fc.get("emboss") or {}
    if fc.get("enabled") and emb.get("on") and float(emb.get("depth", 0)) > 0:
        pad_ = int(math.ceil(float(emb.get("blur", 0)) * 3)) + 4
        r2 = path.boundingRect().toAlignedRect().adjusted(-pad_, -pad_, pad_, pad_)
        cp.drawImage(r2.topLeft(), emboss_image(path, r2, emb))
    # カットアウトシャドー（文字の面の内側に落ちる影）
    ins = t.get("inner") or {}
    if fc.get("enabled") and ins.get("enabled") and float(ins.get("opacity", 0)) > 0:
        blur = float(ins.get("blur", 0))
        dist = float(ins.get("distance", 0))
        ang = math.radians(float(ins.get("angle", 45)))
        dx, dy = math.cos(ang) * dist, math.sin(ang) * dist
        pad_ = int(math.ceil(dist + blur * 3)) + 4
        r2 = path.boundingRect().toAlignedRect().adjusted(-pad_, -pad_, pad_, pad_)
        m = _new(r2)
        mp = _painter(m)
        mp.fillRect(m.rect(), Qt.white)
        mp.translate(-r2.x(), -r2.y())
        mp.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        mp.fillPath(path.translated(dx, dy), Qt.black)      # 面をずらして抜いた残り = 影になる所
        mp.end()
        img = blur_tint(m, blur, ins.get("color", "#000000"))
        fm = _new(r2)                                         # 面の形（この外は消す）
        fp = _painter(fm)
        fp.translate(-r2.x(), -r2.y())
        fp.fillPath(path, Qt.white)
        fp.end()
        ip = _painter(img)
        ip.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        ip.drawImage(0, 0, fm)                                # 面の中だけ残す（fillPath だと外が消えない）
        ip.end()
        cp.save()
        cp.setOpacity(float(ins.get("opacity", 0.6)))
        cp.drawImage(r2.topLeft(), img)
        cp.restore()
    cp.end()
    return canvas, info


def render_doc(doc, cache=None, only=None, skip=None):
    """全テロップを合成。cache: dict(キー→(img,info))で変更のないテロップは再描画しない
    only/skip: 描画する/しないテロップの番号の集合"""
    import json as _json
    W, H = int(doc["canvas"][0]), int(doc["canvas"][1])
    canvas = QImage(W, H, FMT)
    canvas.fill(0)
    p = QPainter(canvas)
    infos = []
    for i, o in enumerate(doc["objects"]):
        key = _json.dumps(o, sort_keys=True, ensure_ascii=False)
        if cache is not None and key in cache:
            img, info = cache[key]
        else:
            img, info = render(o)
            if cache is not None:
                cache[key] = (img, info)
        infos.append(info)
        if (only is not None and i not in only) or (skip is not None and i in skip):
            continue
        p.drawImage(0, 0, img)
    p.end()
    if cache is not None and len(cache) > 200:
        cache.clear()
    return canvas, infos


def render_tall(o, horizontal=False):
    """ロール／クロールのプレビュー用: 文字ブロック全体が入る縦長（横長）の画像
    -> (画像, top)  画像の y=0（クロールは x=0）がキャンバスの top に当たる（移動量を足して描く）
    固定の座布団（free）は流れないので入れない（render_box で別に描く）"""
    import copy
    t = copy.deepcopy(o)
    W, H = int(t["canvas"][0]), int(t["canvas"][1])
    b = image_xf(t).mapRect(image_rect(t)) if is_image(t) else block_rect(t)
    m = 20.0 + sum(float(e.get("width", 0)) for e in t["edges"] if e.get("enabled"))
    for a in char_styles(t):
        for v in ((a or {}).get("ew") or {}).values():
            m = max(m, 20.0 + float(v or 0))
    sh = t.get("shadow") or {}
    if sh.get("enabled"):
        m += float(sh.get("distance", 0)) + float(sh.get("blur", 0)) * 3
    ex = t.get("extrude") or {}
    if ex.get("enabled"):
        m += float(ex.get("depth", 0))
    bx = t.get("box") or {}
    if bx.get("enabled") and bx.get("mode", "fit") == "fit":
        m += max(float(bx.get("pady", 0)), float(bx.get("padx", 0)))
    if bx.get("mode") == "free":
        t["box"]["enabled"] = False
    if horizontal:
        left = math.floor(min(0.0, b.left() - m))
        right = math.ceil(max(float(W), b.right() + m))
        t["pos"] = [t["pos"][0] - left, t["pos"][1]]
        t["canvas"] = [int(right - left), H]
        img, info = render(t)
        return img, left
    top = math.floor(min(0.0, b.top() - m))
    bottom = math.ceil(max(float(H), b.bottom() + m))
    t["pos"] = [t["pos"][0], t["pos"][1] - top]
    t["canvas"] = [W, int(bottom - top)]
    img, info = render(t)
    return img, top


def render_box(o):
    """固定の座布団（free）だけの画像。なければ None"""
    bx = o.get("box") or {}
    if not (bx.get("enabled") and bx.get("mode") == "free"):
        return None
    return render(o, parts=("box",))[0]

