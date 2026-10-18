"""DaVinci Resolve の Text+（Fusionタイトル）を Claps! Titler のテロップに変換する

Text+ の数値は Fusion 独自の単位なので、以下の換算で近似する
  ・文字サイズ: Size × 画面幅 × 0.8 = 文字サイズ(px)（実測で確認: 0.08@1920px → 約122px）
  ・縁の太さ  : Thickness × 文字サイズ(px) × 0.9（ソフト付きの縁は不透明度×0.6）
  ※ 実際の Text+（ゴージャス02）と画素比較して係数を合わせた
  ・影のずれ  : Offset × 画面幅/高さ（Fusion は上が＋なので Y を反転）
"""
import math

from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF

from . import model

ELEMENT_KEYS = ["Enabled", "ElementShape", "Type", "ShadingMappingAngle", "Red", "Green", "Blue", "Alpha", "Opacity",
                "Thickness", "OutsideOnly", "PriorityBack", "Offset", "SoftnessX", "SoftnessY",
                "SoftnessGlow", "Level", "ExtendHorizontal", "ExtendVertical", "Round"]
TOOL_KEYS = ["StyledText", "Font", "Style", "Size", "Center", "CharacterSpacing", "LineSpacing",
             "HorizontalJustificationNew", "VerticalJustificationNew", "Width", "Height"]


def _hex(r, g, b):
    c = QColor()
    c.setRgbF(max(0, min(1, r)), max(0, min(1, g)), max(0, min(1, b)))
    return c.name()


def _match_family(name):
    """Fusion のフォント名を Qt のファミリー名に合わせる（空白・大小文字の違いを吸収）"""
    fams = QFontDatabase.families()
    if name in fams:
        return name
    key = name.replace(" ", "").lower()
    for f in fams:
        if f.replace(" ", "").lower() == key:
            return f
    for f in fams:
        if key in f.replace(" ", "").lower():
            return f
    return name


def _vec(v):
    if isinstance(v, dict):
        return float(v.get(1, v.get("1", 0.0))), float(v.get(2, v.get("2", 0.0)))
    return 0.0, 0.0


def _stops(grad):
    """Fusion のグラデーション値 → [[位置, "#rrggbb", 不透明度], ...]"""
    out = []
    if not isinstance(grad, dict):
        return out
    for pos, c in sorted(((float(k), v) for k, v in grad.items()), key=lambda x: x[0]):
        if isinstance(c, dict):
            r, g, b = (float(c.get(i, c.get(str(i), 0))) for i in (1, 2, 3))
            a = float(c.get(4, c.get("4", 1)))
            out.append([pos, _hex(r, g, b), a])
    return out


def convert(tp, canvas):
    """tp: resolve_bridge.read_textplus() の1要素(dict) → テロップ(dict)"""
    W, H = canvas
    t = model.new_title(W, H)
    t["text"] = str(tp.get("StyledText") or "").replace("\r\n", "\n").replace("\r", "\n")

    fam = _match_family(str(tp.get("Font") or t["font"]["family"]))
    style = str(tp.get("Style") or "Regular")
    styles = QFontDatabase.styles(fam)
    if styles and style not in styles:
        style = styles[0]
    t["font"]["family"], t["font"]["style"] = fam, style

    # 文字サイズ（Fusion は Size×画面幅 を 72/90 倍した値が実際の文字サイズになる）
    size_px = float(tp.get("Size", 0.08)) * W * 0.8
    t["font"]["size"] = max(4, int(round(size_px)))
    cs = float(tp.get("CharacterSpacing", 1.0))
    t["font"]["tracking"] = round((cs - 1.0) * size_px, 1)
    # 行間: Fusion の行送り(LineSpacing×文字サイズ×0.955) をこちらの「行の高さ×倍率」に換算
    fq = QFontDatabase.font(fam, style, 12)
    fq.setPixelSize(max(1, int(round(size_px))))
    ls = QFontMetricsF(fq).lineSpacing()
    t["font"]["leading"] = round(float(tp.get("LineSpacing", 1.0)) * 0.955 * size_px / max(1.0, ls), 3)

    hj = int(round(float(tp.get("HorizontalJustificationNew", 3))))
    t["font"]["align"] = {0: "left", 2: "right"}.get(hj, "center")

    cx, cy = _vec(tp.get("Center"))
    # Fusion は文字の縦位置がわずかに下になる（実測: 文字サイズの約5.7%）
    t["pos"] = [cx * W, (1.0 - cy) * H + size_px * 0.057]

    # シェーディング要素を「奥→手前」に並べる（PriorityBack が小さいほど奥）
    elems = [e for e in tp.get("elements", []) if e.get("Enabled")]
    elems.sort(key=lambda e: float(e.get("PriorityBack", 0)))

    fill, outlines, shadows, borders = None, [], [], []
    for e in elems:
        shape = int(round(float(e.get("ElementShape", 0))))
        ox, oy = _vec(e.get("Offset"))
        if shape == 0 and (abs(ox) > 1e-4 or abs(oy) > 1e-4):
            shadows.append(e)
        elif shape == 0:
            fill = e                 # 一番手前の文字塗りを面とする
        elif shape == 1:
            outlines.append(e)
        elif shape in (2, 3):
            borders.append(e)

    def color_of(e):
        return _hex(float(e.get("Red", 1)), float(e.get("Green", 1)), float(e.get("Blue", 1)))

    def alpha_of(e):
        return max(0.0, min(1.0, float(e.get("Alpha", 1)) * float(e.get("Opacity", 1))))

    def soft_of(e):
        return round(max(float(e.get("SoftnessX", 0)), float(e.get("SoftnessY", 0))) * 0.9, 1)

    # 面
    if fill is not None:
        t["face"]["enabled"] = True
        t["face"]["color"] = color_of(fill)
        t["face"]["opacity"] = alpha_of(fill)
        stops = _stops(fill.get("gradient")) if int(round(float(fill.get("Type", 0)))) == 2 else []
        if len(stops) >= 2:
            t["face"]["gradient"] = True
            t["face"]["stops"] = stops
            t["face"]["color"], t["face"]["color2"] = stops[0][1], stops[-1][1]
            t["face"]["angle"] = int(round(270 - float(fill.get("ShadingMappingAngle", 0) or 0))) % 360
            t["face"]["span"] = "line"
        else:
            t["face"]["gradient"] = False
    else:
        t["face"]["enabled"] = False

    # 縁: 手前のものほど文字に近い（エッジ1）。Text+ は各縁を文字の輪郭から測るので差分を太さにする
    inner_first = list(reversed(outlines))[:model.MAX_EDGES]
    prev = 0.0
    edges = []
    for e in inner_first:
        th = float(e.get("Thickness", 0.02)) * size_px * 0.9
        if not float(e.get("OutsideOnly", 0)):
            th = th / 2.0
        w = max(0.5, th - prev)
        prev = max(prev, th)
        soft = soft_of(e)
        op = alpha_of(e) * (0.6 if soft > 0 else 1.0)    # ソフトな縁は Fusion の方が淡く出る
        edges.append({"enabled": True, "width": round(w, 1), "color": color_of(e),
                      "opacity": round(op, 2), "soft": soft})
    while len(edges) < model.MAX_EDGES:
        d = dict(model.DEFAULT_TITLE["edges"][len(edges)])
        d["enabled"] = False
        edges.append(d)
    t["edges"] = edges

    # 影（オフセット付きの塗り要素の最初の1つ）
    if shadows:
        s = shadows[0]
        ox, oy = _vec(s.get("Offset"))
        dx, dy = ox * W, -oy * H
        t["shadow"].update({
            "enabled": True, "color": color_of(s), "opacity": alpha_of(s),
            "distance": round(math.hypot(dx, dy), 1),
            "angle": int(round(math.degrees(math.atan2(dy, dx)))) % 360,
            "blur": soft_of(s),
        })
    else:
        t["shadow"]["enabled"] = False

    # 座布団（ボーダーフィル）
    if borders:
        b = borders[0]
        t["box"].update({
            "enabled": True, "color": color_of(b), "opacity": alpha_of(b), "mode": "fit",
            "padx": int(round(float(b.get("ExtendHorizontal", 0.2)) * size_px)),
            "pady": int(round(float(b.get("ExtendVertical", 0.1)) * size_px)),
            "radius": int(round(float(b.get("Round", 0)) * size_px)),
        })
    else:
        t["box"]["enabled"] = False

    t["extrude"]["enabled"] = False
    # 左/右揃えは Text+ の Center が文字の端なので、こちらの「ブロック中心」に直す
    if t["font"]["align"] in ("left", "right") and t["text"].strip():
        from . import render as R
        _, block, _ = R.text_path(t)
        t["pos"][0] += block.width() / 2 if t["font"]["align"] == "left" else -block.width() / 2
    return t
