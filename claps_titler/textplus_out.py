"""Claps! Titler のテロップを、Fusion の Text+ ノード構成として組み立てる（textplus.py の逆変換）

構成（テロップ1つ分）:
  [座布団(固定)] Background + RectangleMask
  [押し出し]     Text+(シルエット) → Duplicate(1pxずつ奥行き分) → Background のマスク
                 （いただいた「キャストシャドウテキスト」マクロと同じ方式）
  [本体]         Text+（面・縁・影・座布団(文字に合わせる) を Text+ の要素で表現）
これらを Merge で重ね、複数テロップはさらに Merge で積んで MediaOut へ。
"""
import math

from PySide6.QtGui import QColor, QFontMetricsF

from . import render as R
from . import roll as RL

FUSION = None      # resolve_bridge が接続時に入れる（Loader のファイル選択を止めるのに使う）
from . import anim

TAG = "ClapsTitler"
SIZE_K = 0.8          # textplus.py と同じ換算係数
EDGE_K = 0.615   # 縁（外側のみ）: Fusion の Thickness×文字サイズ×0.615 = 実際の太さ(px)（実測）
SOFT_K = 0.9
SOFT_OPACITY = 0.6
Y_SHIFT = 0.057
DIRBLUR_K = 1.0       # 押し出しの長さ換算（Length = 奥行きpx / 画面幅 × これ）
LINE_K = 1.0          # Fusion の行送り = LineSpacing × 文字サイズ（多行で実測して一致）


def fusion_line_spacing(o):
    """こちらの行間(倍) → Text+ の LineSpacing"""
    sp = max(1.0, size_px(o))
    fm = QFontMetricsF(R.make_font(o["font"]))
    return float(o["font"].get("leading", 1.2)) * fm.lineSpacing() / (LINE_K * sp)


def _rgb(hex_):
    c = QColor(hex_)
    return c.redF(), c.greenF(), c.blueF()


def _tag(gid, uid):
    return f"{TAG}:{gid}:{uid}"


def parse_tag(s):
    """コメント文字列 → (gid, uid) / 該当なしなら None"""
    if not s or not str(s).startswith(TAG + ":"):
        return None
    parts = str(s).split(":")
    if len(parts) >= 3:
        return parts[1], parts[2]
    return None


def size_px(o):
    return float(o["font"]["size"])


def text_inputs(o, W, H):
    """Text+ の基本入力（文字・フォント・位置など）"""
    sp = size_px(o)
    align = o["font"].get("align", "center")
    block = R.block_rect(o)              # アンカーを考慮した文字ブロック
    x, y = block.center().x(), block.center().y()
    # 左/右揃えのとき Text+ は Center を文字の左端/右端として扱う
    if align in ("left", "right") and o["text"].strip():
        x = block.left() if align == "left" else block.right()
    d = {
        "StyledText": o["text"],
        "Font": o["font"]["family"],
        "Style": o["font"].get("style", "Regular"),
        "Size": sp / (W * SIZE_K),
        "CharacterSpacing": 1.0 + float(o["font"].get("tracking", 0)) / max(1.0, sp),
        "FontFeatures": "",
        "LineSpacing": fusion_line_spacing(o),
        "Center": {1: x / W, 2: 1.0 - (y - sp * Y_SHIFT) / H, 3: 0.0},
        "Width": W, "Height": H,
        "UseFrameFormatSettings": 0,
    }
    # 行揃え（Fusion: 0=左 1=右? の実装差を避けるため、左/右揃えは Left/Right の個別フラグも立てる）
    if align == "left":
        d.update({"HorizontalJustificationNew": 0, "HorizontalLeftCenterRight": -1})
    elif align == "right":
        d.update({"HorizontalJustificationNew": 2, "HorizontalLeftCenterRight": 1})
    else:
        d.update({"HorizontalJustificationNew": 3, "HorizontalLeftCenterRight": 0})
    return d


# Text+ の雛形（Template）には要素ごとに初期値が入っている（要素3は影用のずれ等）。
# 使う要素は、指定しない項目もここで既定値に戻す
_ELEM_RESET = {"Offset": {1: 0.0, 2: 0.0, 3: 0.0}, "SoftnessX": 0.0, "SoftnessY": 0.0, "Softness": 0,
               "OutsideOnly": 0, "Thickness": 0.02, "Level": 3, "Type": 0, "Alpha": 1.0,
               "CleanIntersections": 1}     # 字形の線の重なりをまとめてから縁を付ける（角の段差を防ぐ）


def _elem(i, **kw):
    v = dict(_ELEM_RESET)
    v.update(kw)
    return {f"{k}{i}": x for k, x in v.items()}


def edge_plan(o):
    """Text+ の縁の要素の割り当て -> [(縁番号, 要素番号, 全体での累計の太さpx, 全体で使うか)]
    文字ごとの指定だけで使う縁も要素を作り、使わない文字は文字ごとの指定で消す"""
    used = R.used_edges(o)
    out, cum = [], 0.0
    for p_, i in enumerate(used[:4]):
        e = o["edges"][i]
        on = bool(e.get("enabled") and e.get("width", 0) > 0)
        w = float(e.get("width", 0)) if on else max(1.0, float(e.get("width", 0) or 0))
        cum_here = cum + w
        if on:
            cum = cum_here
        out.append((i, 2 + p_, cum_here, on))
    return out


def shadow_plan(o):
    """影の要素を作るか -> (作るか, 全体で出すか, どれかの文字がぼかしを指定しているか)"""
    sh = o["shadow"]
    glob_on = bool(sh.get("enabled") and sh.get("opacity", 0) > 0)
    any_on, any_blur = glob_on, False
    for r in o.get("runs") or []:
        if r.get("sh_on"):
            any_on = True
        if float(r.get("sh_blur", 0) or 0) > 0:
            any_blur = True
    return any_on, glob_on, any_blur


def _max_cum(o):
    m = sum(float(e["width"]) for e in o["edges"] if e.get("enabled") and e.get("width", 0) > 0)
    for a in R.char_styles(o):
        if a:
            m = max(m, R.seg_edges(o, a)[1])
    return m


def element_inputs(o, W, H, silhouette=False, facemask=False):
    """Text+ のシェーディング要素（最大8）。PriorityBack が大きいほど手前"""
    sp = max(1.0, size_px(o))
    d = {}
    for i in range(1, 9):
        d[f"Enabled{i}"] = 0
    cum = sum(float(e["width"]) for e in o["edges"] if e.get("enabled") and e.get("width", 0) > 0)
    plan = edge_plan(o)
    mcum = _max_cum(o)

    if facemask:       # 画像で塗る範囲・内側の影の型: 面だけ白（soft はぼかし px）
        soft = float(facemask) if not isinstance(facemask, bool) else 0.0
        d.update(_elem(1, Enabled=1, ElementShape=0, Type=0, Red=1, Green=1, Blue=1, Alpha=1, Opacity=1,
                       PriorityBack=8, SoftnessX=soft / SOFT_K, SoftnessY=soft / SOFT_K,
                       Softness=1 if soft > 0 else 0))
        return d

    if silhouette:     # 押し出し用: 面＋一番外の縁まで白一色
        d.update(_elem(1, Enabled=1, ElementShape=0, Type=0, Red=1, Green=1, Blue=1, Alpha=1, Opacity=1,
                       PriorityBack=8))
        if mcum > 0:
            d.update(_elem(2, Enabled=1, ElementShape=1, OutsideOnly=1, Type=0, Red=1, Green=1, Blue=1, Alpha=1,
                           Opacity=1, Thickness=cum / (sp * EDGE_K), PriorityBack=7))
        return d

    # 1: 面
    fc = o["face"]
    if fc.get("enabled"):
        r, g, b = _rgb(fc["color"])
        d.update(_elem(1, Enabled=1, ElementShape=0, Red=r, Green=g, Blue=b, Alpha=1,
                       Opacity=float(fc.get("opacity", 1)), PriorityBack=8, Type=0))
        if fc.get("gradient") and len(fc.get("stops") or []) >= 2:
            d["Type1"] = 2
            d["ShadingMappingAngle1"] = (270 - float(fc.get("angle", 90))) % 360   # 実測: 0=下→上、反時計回り
            d["__gradient1"] = fc["stops"]
            if fc.get("span", "line") == "block":
                d["ShadingMappingLevel1"] = 1      # 文字全体（Text）
    # 2〜5: 縁（内側ほど手前）
    char_soft = {}
    for a in R.char_styles(o):
        for kk, v in ((a or {}).get("es") or {}).items():
            if float(v) > 0:
                char_soft[int(kk)] = True
    for k, (ei, el, c, on) in enumerate(plan):
        e = o["edges"][ei]
        r, g, b = _rgb(e["color"])
        soft = float(e.get("soft", 0))
        op = float(e.get("opacity", 1))
        if soft > 0:
            op = min(1.0, op / SOFT_OPACITY)
        sflag = 1 if (soft > 0 or char_soft.get(ei)) else 0
        d.update(_elem(el, Enabled=1, ElementShape=1, OutsideOnly=1, Type=0, Red=r, Green=g, Blue=b, Alpha=1,
                       Opacity=op, Thickness=c / (sp * EDGE_K), PriorityBack=7 - k,
                       SoftnessX=soft / SOFT_K, SoftnessY=soft / SOFT_K, Softness=sflag,
                       JoinStyle=1))
        if R.edge_grad_on(e):                 # 縁のグラデーション
            d[f"Type{el}"] = 2
            d[f"ShadingMappingAngle{el}"] = (270 - float(e.get("angle", 90))) % 360
            d[f"__gradient{el}"] = e["stops"]
            if e.get("span", "line") == "block":
                d[f"ShadingMappingLevel{el}"] = 1
    # 7: 座布団（文字に合わせる）
    bx = o["box"]
    if bx.get("enabled") and bx.get("mode", "fit") == "fit":
        r, g, b = _rgb(bx["color"])
        d.update(_elem(7, Enabled=1, ElementShape=2, Type=0, Red=r, Green=g, Blue=b, Alpha=1,
                       Opacity=float(bx["opacity"]), PriorityBack=1, Level=1,
                       ExtendHorizontal=float(bx["padx"]) / sp, ExtendVertical=float(bx["pady"]) / sp,
                       Round=float(bx["radius"]) / sp))
    return d


def shadow_groups(o):
    """影の組（距離・方向が同じ文字ごと）-> [(距離, 方向)]。全体の設定の組が先頭"""
    styles = R.char_styles(o) or [{}]
    keys = []
    for a in styles:
        on, col, blur, op, dist, ang = R.seg_shadow(o, a)
        if on and (dist, ang) not in keys:
            keys.append((dist, ang))
    if not (o.get("text") or "").strip():
        return []
    return keys


def _shadow_dup(o):
    ex = o["extrude"]
    return bool(ex.get("enabled") and ex.get("depth", 0) > 0)


def shadow_inputs(o, W, H):
    """影用の Text+: 面＋一番外の縁までを影の色で（ずらしは後の Transform で）
    押し出しを含めるときは不透明で重ね、最後に不透明度を掛ける（重ねて濃くならないように）"""
    sp = max(1.0, size_px(o))
    sh = o["shadow"]
    s_op = 1.0 if _shadow_dup(o) else float(sh["opacity"])
    d = {}
    for i in range(1, 9):
        d[f"Enabled{i}"] = 0
    r, g, b = _rgb(sh["color"])
    blur = float(sh.get("blur", 0))
    any_blur = any(float((a or {}).get("sh_blur", 0) or 0) > 0 for a in R.char_styles(o))
    sflag = 1 if (blur > 0 or any_blur) else 0
    cum = sum(float(e["width"]) for e in o["edges"] if e.get("enabled") and e.get("width", 0) > 0)
    d.update(_elem(1, Enabled=1, ElementShape=0, Type=0, Red=r, Green=g, Blue=b, Alpha=1,
                   Opacity=s_op, PriorityBack=8,
                   SoftnessX=blur / SOFT_K, SoftnessY=blur / SOFT_K, Softness=sflag))
    if _max_cum(o) > 0:
        d.update(_elem(2, Enabled=1, ElementShape=1, OutsideOnly=1, Type=0, Red=r, Green=g, Blue=b, Alpha=1,
                       Opacity=s_op, PriorityBack=7, Thickness=cum / (sp * EDGE_K),
                       SoftnessX=blur / SOFT_K, SoftnessY=blur / SOFT_K, Softness=sflag, JoinStyle=1))
    return d


def add_duplicate(comp, src, o, W, H, col, row):
    """押し出しと同じ向き・奥行きで 1px ずつ重ねる（Duplicate が無ければ None）"""
    ex = o["extrude"]
    depth = max(1, int(round(float(ex["depth"]))))
    a = math.radians(float(ex["angle"]))
    db = comp.AddTool("Fuse.Duplicate", col, row)
    if db is None:
        return None
    apply_inputs(db, {"Copies": depth,
                      "Center": {1: 0.5 + math.cos(a) / W, 2: 0.5 - math.sin(a) / H, 3: 0.0},
                      "MergeUnder": 1})
    db.ConnectInput("Background", src)
    return db


def gradient_value(stops):
    """[[位置, 色, 不透明度]...] → Fusion のグラデーション値"""
    out = {}
    for st in sorted(stops, key=lambda s: float(s[0])):
        r, g, b = _rgb(st[1])
        a = float(st[2]) if len(st) > 2 else 1.0
        out[float(st[0])] = {1: r, 2: g, 3: b, 4: a}
    return out


def apply_inputs(tool, d):
    gradients = {k: v for k, v in d.items() if k.startswith("__gradient")}
    for k, v in d.items():
        if k.startswith("__"):
            continue
        try:
            tool.SetInput(k, v)
        except Exception:
            pass
    for k, stops in gradients.items():
        i = k.replace("__gradient", "")
        try:
            tool.SetInput(f"ShadingGradient{i}", gradient_value(stops))
        except Exception:
            pass


# ---------------- 文字ごとの指定（Character Level Styling）
# 実測で確かめた Fusion の内部番号: {番号, 開始, 終了(含む), Value=値, Index=要素番号(0=1つ目)}
CLS_SIZE = 102
CLS_FONT, CLS_STYLE = 100, 109          # フォント名・スタイル名（String = "..." で渡す）
CLS_RED, CLS_GREEN, CLS_BLUE = 2401, 2402, 2403


def _lua_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n") + '"'


CLS_ENABLED, CLS_OPACITY, CLS_THICK = 2000, 2010, 2200
CLS_TYPE = 2400
CLS_SPACING = 1300                      # 文字の後ろの間隔（1.0=標準、+1.0で1em空く）
CLS_OFFSET_X, CLS_OFFSET_Y = 1350, 1351 # 文字のずらし（画面の幅に対する割合、Y は上が＋）                         # 塗りの種類（0=単色, 2=グラデーション）
CLS_SOFT_X, CLS_SOFT_Y = 2500, 2501


def cls_entries(o, W, silhouette=False, shadow=None, facemask=False):
    """runs → CharacterLevelStyling の配列（文字列のリスト）。Index = 要素番号 − 1
    silhouette=True は押し出し用の白い形（大きさ・フォント・外形の太さだけ）"""
    styles = R.char_styles(o)
    size_val = size_px(o) / (W * SIZE_K)
    sp = max(1.0, size_px(o))
    plan = edge_plan(o)
    pos_of = {ei: (el, c, on) for ei, el, c, on in plan}
    gcum = sum(float(e["width"]) for e in o["edges"] if e.get("enabled") and e.get("width", 0) > 0)
    mcum = _max_cum(o)
    make_sh, _g, _b = shadow_plan(o)
    sh = o["shadow"]
    g_sh = R.seg_shadow(o, {})
    out = []

    def ent(cid, i, j, v, idx=None):
        if idx is None:
            out.append("{ %d, %d, %d, Value = %.6f }" % (cid, i, j, v))
        else:
            out.append("{ %d, %d, %d, Value = %.6f, Index = %d }" % (cid, i, j, v, idx))

    def color(i, j, hexc, idx):
        r, g, b = _rgb(hexc)
        for cid, v in ((CLS_RED, r), (CLS_GREEN, g), (CLS_BLUE, b)):
            ent(cid, i, j, v, idx)

    i, n = 0, len(styles)
    while i < n:
        a = styles[i]
        j = i
        while j + 1 < n and R._seg_key(styles[j + 1]) == R._seg_key(a):
            j += 1
        se, sc = R.seg_edges(o, a)
        # 大きさ・フォント
        sz = float(a.get("size", 1))
        if abs(sz - 1) > 1e-6:
            ent(CLS_SIZE, i, j, size_val * sz)
        fs = a.get("font")
        if fs:
            out.append("{ %d, %d, %d, String = %s }" % (CLS_FONT, i, j, _lua_str(str(fs[0]))))
            if len(fs) > 1 and fs[1]:
                out.append("{ %d, %d, %d, String = %s }" % (CLS_STYLE, i, j, _lua_str(str(fs[1]))))
        bs = float(a.get("bs", 0) or 0)
        if bs:
            ent(CLS_OFFSET_Y, i, j, bs / W)
        if shadow is not None:          # 影用の Text+（この組の距離・方向の文字だけ出す）
            on, col, blur, op, dist, ang = R.seg_shadow(o, a)
            idxs = [0] + ([1] if mcum > 0 else [])
            if not (on and (dist, ang) == tuple(shadow)):
                for idx in idxs:
                    ent(CLS_ENABLED, i, j, 0, idx)
            else:
                for idx in idxs:
                    if col != sh["color"]:
                        color(i, j, col, idx)
                    if abs(blur - g_sh[2]) > 1e-6:
                        ent(CLS_SOFT_X, i, j, blur / SOFT_K, idx)
                        ent(CLS_SOFT_Y, i, j, blur / SOFT_K, idx)
                    og = float(sh.get("opacity", 0))
                    if abs(op - og) > 1e-6:
                        ent(CLS_OPACITY, i, j, min(1.0, op / max(og, 1e-3)) if _shadow_dup(o) else op, idx)
                if mcum > 0:
                    if sc <= 0:
                        ent(CLS_ENABLED, i, j, 0, 1)
                    elif abs(sc - gcum) > 1e-6:
                        ent(CLS_THICK, i, j, sc / (sp * EDGE_K), 1)
            i = j + 1
            continue
        if facemask:                    # 画像で塗る範囲: 自分の色を指定した文字は外す（"all" は形だけ）
            if a.get("face") and facemask != "all":
                ent(CLS_ENABLED, i, j, 0, 0)
            i = j + 1
            continue
        if silhouette:
            if mcum > 0:
                if sc <= 0:
                    ent(CLS_ENABLED, i, j, 0, 1)
                elif abs(sc - gcum) > 1e-6:
                    ent(CLS_THICK, i, j, sc / (sp * EDGE_K), 1)
            i = j + 1
            continue
        # 面の色
        if a.get("face"):
            if o["face"].get("gradient"):
                ent(CLS_TYPE, i, j, 0, 0)          # グラデーションの面でも、この文字は単色に
            color(i, j, a["face"], 0)
        # 縁: 太さ（累計）・出す/出さない・色
        have = {x[0]: x for x in se}
        for ei, (el, gc, gon) in pos_of.items():
            idx = el - 1
            if ei not in have:
                ent(CLS_ENABLED, i, j, 0, idx)
                continue
            _, e, col, c = have[ei]
            if abs(c - gc) > 1e-6 or not gon:
                ent(CLS_THICK, i, j, c / (sp * EDGE_K), idx)
            ge = o["edges"][ei]
            if str(ei) in (a.get("es") or {}) and abs(float(e.get("soft", 0)) - float(ge.get("soft", 0))) > 1e-6:
                ent(CLS_SOFT_X, i, j, float(e["soft"]) / SOFT_K, idx)
                ent(CLS_SOFT_Y, i, j, float(e["soft"]) / SOFT_K, idx)
            if str(ei) in (a.get("eo") or {}) or str(ei) in (a.get("es") or {}):
                op_c = float(e.get("opacity", 1))
                if float(e.get("soft", 0)) > 0:
                    op_c = min(1.0, op_c / SOFT_OPACITY)
                op_g = float(ge.get("opacity", 1))
                if float(ge.get("soft", 0)) > 0:
                    op_g = min(1.0, op_g / SOFT_OPACITY)
                if abs(op_c - op_g) > 1e-6:
                    ent(CLS_OPACITY, i, j, op_c, idx)
            if (a.get("edges") or {}).get(str(ei)):
                if R.edge_grad_on(ge):
                    ent(CLS_TYPE, i, j, 0, idx)    # グラデーションの縁でも、この文字は単色に
                color(i, j, col, idx)
        i = j + 1
    # 文字の後ろの間隔（手のカーニング＋縁の太さの差の自動の空き）は1文字ずつ
    for ci, kv in enumerate(R.char_kerns(o)):
        if abs(kv) > 1e-6:
            szc = float((styles[ci] or {}).get("size", 1))
            trk = float(o["font"].get("tracking", 0) or 0) * szc
            ent(CLS_SPACING, ci, ci, 1.0 + (trk + kv) / max(1.0, sp * szc))
    return out


def attach_cls(comp, text_tools, o, W, col=0, silhouette=False, shadow=None, tag="", facemask=False):
    """Character Level Styling のノードを作って、文字のノードにつなぐ（Fusion の Lua で設定）"""
    import time as _t
    name = f"CT_CLS{'s' if silhouette else ''}{tag}_{o['uid']}"
    entries = cls_entries(o, W, silhouette, shadow, facemask)
    if not entries:
        return None
    settings = ("{ Tools = ordered() { __NAME__ = StyledTextCLS { Inputs = { Text = Input { Value = %s }, "
                "CharacterLevelStyling = Input { Value = StyledText { Array = { %s }, Value = \"\" } } } } } }"
                % (_lua_str(o["text"]), ", ".join(entries)))
    token = f"done_{o['uid']}_{_t.time():.3f}"
    comp.SetData("ct_cls_settings", settings)
    comp.SetData("ct_cls_done", "")
    lua = ('local t2 = comp:AddTool("StyledTextCLS", %d, 0)\n'
           'local s = comp:GetData("ct_cls_settings")\n'
           's = string.gsub(s, "__NAME__", t2.Name)\n'
           't2:LoadSettings(bmd.readstring(s))\n'
           't2:SetAttrs({TOOLS_Name = "%s"})\n'
           'comp:SetData("ct_cls_done", "%s")\n') % (col * 2 - 1 - (1 if silhouette else 0), name, token)
    comp.Execute(lua)
    for _ in range(100):                        # Execute は非同期なので終わるまで待つ
        if comp.GetData("ct_cls_done") == token:
            break
        _t.sleep(0.03)
    cls = comp.FindTool(name)
    if cls is None:
        return None
    for tt in text_tools:
        try:
            tt.ConnectInput("StyledText", cls)
        except Exception:
            pass
    return cls


def build(comp, doc, gid, only_uid=None):
    """comp の中身を doc のテロップで組み直す（自分が作ったノードだけ消して作り直す）"""
    W, H = int(doc["canvas"][0]), int(doc["canvas"][1])
    comp.Lock()
    try:
        tools = comp.GetToolList(False) or {}
        mediaout = None
        template = None
        for t in list(tools.values()):
            reg = t.GetAttrs().get("TOOLS_RegID")
            name = t.GetAttrs().get("TOOLS_Name")
            if reg == "MediaOut":
                mediaout = t
                continue
            if reg == "MediaIn":
                continue
            if reg == "TextPlus" and name == "Template":
                template = t        # タイトルの Inspector に出る Text+ は残して使う
                continue
            t.Delete()              # 以前の Claps ノードを消す
        if mediaout is None:
            mediaout = comp.AddTool("MediaOut", 10, 0)

        top = None       # 積み上げた結果
        col = 0
        try:
            fps = float(comp.GetPrefs("Comp.FrameFormat.Rate") or 29.97)
        except Exception:
            fps = 29.97
        objs = doc["objects"] if only_uid is None else [o for o in doc["objects"] if o["uid"] == only_uid]
        gcen = group_center(doc) if (doc.get("ganim") or {}).get("enabled") else None
        for o in objs:
            if R.is_image(o):                 # 画像のテロップ
                layer = add_image(comp, doc, o, W, H, col)
                eo, center = anim.effective(doc, o, gcen)
                layer = anim.fusion_add(comp, layer, eo, W, H, fps, center, col, 10)
                if top is None:
                    top = layer
                else:
                    mg = comp.AddTool("Merge", col, 0)
                    mg.ConnectInput("Background", top)
                    mg.ConnectInput("Foreground", layer)
                    top = mg
                col += 2
                continue
            layer = None
            sil = None
            ttools = []          # この行の文字のノード（ロールで一緒に動かす）
            bx = o["box"]
            # 座布団（固定）
            if bx.get("enabled") and bx.get("mode") == "free":
                cx, cy, bw, bh = bx["rect"]
                bg = comp.AddTool("Background", col, 3)
                r, g, b = _rgb(bx["color"])
                apply_inputs(bg, {"Width": W, "Height": H, "UseFrameFormatSettings": 0,
                                  "TopLeftRed": r, "TopLeftGreen": g, "TopLeftBlue": b,
                                  "TopLeftAlpha": float(bx["opacity"])})
                m = comp.AddTool("RectangleMask", col, 4)
                apply_inputs(m, {"Center": {1: cx / W, 2: 1 - cy / H, 3: 0}, "Width": bw / W, "Height": bh / H,
                                 "CornerRadius": min(1.0, 2 * float(bx["radius"]) / max(1.0, min(bw, bh))),
                                 "MaskWidth": W, "MaskHeight": H, "UseFrameFormatSettings": 0})
                bg.ConnectInput("EffectMask", m)
                bg.SetAttrs({"TOOLS_Name": f"CT_Box_{o['uid']}"})
                layer = bg
            # 影（距離・方向の組ごとに別の Text+。押し出しの下に置く）
            for gi, (sdist, sang) in enumerate(shadow_groups(o)):
                st = comp.AddTool("TextPlus", col, 11 + gi * 3)
                apply_inputs(st, text_inputs(o, W, H))
                apply_inputs(st, shadow_inputs(o, W, H))
                st.SetAttrs({"TOOLS_Name": f"CT_ShadowText{gi}_{o['uid']}"})
                ttools.append(st)
                if o.get("runs") or gi > 0 or (sdist, sang) != (float(o["shadow"].get("distance", 0)),
                                                                float(o["shadow"].get("angle", 45))) \
                        or not o["shadow"].get("enabled"):
                    attach_cls(comp, [st], o, W, col, shadow=(sdist, sang), tag=f"h{gi}")
                src = st
                exs = o["extrude"]
                if exs.get("enabled") and exs.get("depth", 0) > 0:
                    dup = add_duplicate(comp, st, o, W, H, col, 12 + gi * 3)
                    if dup is not None:
                        src = dup
                sxf = comp.AddTool("Transform", col, 13 + gi * 3)
                a_ = math.radians(sang)
                apply_inputs(sxf, {"Center": {1: 0.5 + math.cos(a_) * sdist / W,
                                              2: 0.5 - math.sin(a_) * sdist / H, 3: 0.0}})
                sxf.ConnectInput("Input", src)
                sxf.SetAttrs({"TOOLS_Name": f"CT_Shadow{gi}_{o['uid']}"})
                if src is not st:               # 重ねた後で不透明度を掛ける
                    bc = comp.AddTool("BrightnessContrast", col, 15 + gi * 3)
                    apply_inputs(bc, {"Gain": float(o["shadow"]["opacity"]), "Alpha": 1})
                    bc.ConnectInput("Input", sxf)
                    sxf = bc
                if layer is None:
                    layer = sxf
                else:
                    mg = comp.AddTool("Merge", col, 14 + gi * 3)
                    mg.ConnectInput("Background", layer)
                    mg.ConnectInput("Foreground", sxf)
                    layer = mg
            # 押し出し（キャストシャドウ方式）
            ex = o["extrude"]
            if ex.get("enabled") and ex.get("depth", 0) > 0 and o["text"].strip():
                sil = comp.AddTool("TextPlus", col, 5)
                apply_inputs(sil, text_inputs(o, W, H))
                apply_inputs(sil, element_inputs(o, W, H, silhouette=True))
                # シルエットを1pxずつずらして奥行きの数だけ重ねる（プレビューと同じ単色の押し出し）
                depth = max(1, int(round(float(ex["depth"]))))
                a = math.radians(float(ex["angle"]))
                db = comp.AddTool("Fuse.Duplicate", col, 6)
                if db is not None:
                    apply_inputs(db, {"Copies": depth,
                                      "Center": {1: 0.5 + math.cos(a) / W, 2: 0.5 - math.sin(a) / H, 3: 0.0},
                                      "MergeUnder": 1})
                    db.ConnectInput("Background", sil)     # Duplicate の入力名は Background
                else:              # Duplicate が無い環境では従来のぼかし方式
                    db = comp.AddTool("DirectionalBlur", col, 6)
                    apply_inputs(db, {"Type": 0, "Length": float(ex["depth"]) / W * DIRBLUR_K,
                                      "Angle": 180.0 - float(ex["angle"]), "Glow": 100})
                    db.ConnectInput("Input", sil)
                if ex.get("grad") and db.GetAttrs().get("TOOLS_RegID") != "DirectionalBlur":
                    ebg = extrude_grad(comp, o, sil, W, H, col)       # 手前→奥のグラデーション（帯を重ねる）
                    db.Delete()
                else:
                    ebg = comp.AddTool("Background", col, 7)
                    r, g, b = _rgb(ex["color"])
                    apply_inputs(ebg, {"Width": W, "Height": H, "UseFrameFormatSettings": 0,
                                       "TopLeftRed": r, "TopLeftGreen": g, "TopLeftBlue": b,
                                       "TopLeftAlpha": float(ex.get("opacity", 1))})
                    ebg.ConnectInput("EffectMask", db)
                sil.SetAttrs({"TOOLS_Name": f"CT_ExtrudeShape_{o['uid']}"})
                ttools.append(sil)
                ebg.SetAttrs({"TOOLS_Name": f"CT_Extrude_{o['uid']}"})
                if layer is None:
                    layer = ebg
                else:
                    mg = comp.AddTool("Merge", col, 8)
                    mg.ConnectInput("Background", layer)
                    mg.ConnectInput("Foreground", ebg)
                    layer = mg
            # 本体（最初のテロップはタイトルの Template をそのまま使う → Inspector で触れる）
            if template is not None:
                tx, template = template, None
            else:
                tx = comp.AddTool("TextPlus", col, 1)
                tx.SetAttrs({"TOOLS_Name": f"CT_Text_{o['uid']}"})
            apply_inputs(tx, text_inputs(o, W, H))
            apply_inputs(tx, element_inputs(o, W, H))
            ttools.append(tx)
            tx.SetInput("Comments", _tag(gid, o["uid"]))
            if o.get("runs") and o["text"].strip():       # 文字ごとの指定 → Character Level Styling
                attach_cls(comp, [tx], o, W, col)
                if sil is not None:                          # 押し出しの形は別の指定（外形の太さ）
                    attach_cls(comp, [sil], o, W, col, silhouette=True)
            if layer is not None:
                mg = comp.AddTool("Merge", col, 2)
                mg.ConnectInput("Background", layer)
                mg.ConnectInput("Foreground", tx)
                layer = mg
            else:
                layer = tx
            # 画像で塗る（面の範囲に画像を重ねる）
            if R.face_img_on(o):
                layer = add_face_image(comp, o, W, H, col, layer, ttools)
            # 面のエンボス
            emb = o["face"].get("emboss") or {}
            if o["face"].get("enabled") and emb.get("on") and float(emb.get("depth", 0)) > 0 and o["text"].strip():
                layer = add_emboss(comp, o, W, H, col, layer, ttools)
            # カットアウトシャドー（面の内側の影）
            ins = o.get("inner") or {}
            if o["face"].get("enabled") and ins.get("enabled") and float(ins.get("opacity", 0)) > 0 \
                    and o["text"].strip():
                layer = add_inner_shadow(comp, o, W, H, col, layer, ttools)
            # ロール／クロール: 文字のノードの位置を時間で動かす（画面の外の文字も描けるように Text+ 自体を動かす）
            if RL.active(o):
                c0 = text_inputs(o, W, H)["Center"]
                ex = RL.fusion_center_expr(o, W, H, c0[1], c0[2])
                for tt in ttools:
                    try:
                        tt.Center.SetExpression(ex)
                    except Exception:
                        pass
            # テロップ自体の回転・拡大
            layer = add_xf(comp, layer, o, W, H, col, 9)
            # 簡易アニメ（イン/アウト。まとめアニメなら全テロップ共通の中心で）
            eo, center = anim.effective(doc, o, gcen)
            layer = anim.fusion_add(comp, layer, eo, W, H, fps, center, col, 10)
            # テロップを積む
            if top is None:
                top = layer
            else:
                mg = comp.AddTool("Merge", col, 0)
                mg.ConnectInput("Background", top)
                mg.ConnectInput("Foreground", layer)
                top = mg
            col += 2
        if template is not None:          # 文字のテロップがなかったとき: Template は空にして目印だけ付ける
            try:
                template.SetInput("StyledText", "")
                if doc["objects"]:
                    template.SetInput("Comments", _tag(gid, doc["objects"][0]["uid"]))
            except Exception:
                pass
        if top is not None:
            mediaout.ConnectInput("Input", top)
    finally:
        comp.Unlock()
    return True


def _loader(comp, path, col, row, name):
    """Loader を足す（ファイル選択が開かないように）"""
    fu = FUSION
    old = None
    try:
        if fu is not None:
            old = fu.GetPrefs("Global.UserInterface.AutoClipBrowse")
            fu.SetPrefs("Global.UserInterface.AutoClipBrowse", False)
        ld = comp.AddTool("Loader", col, row)
    finally:
        if fu is not None and old is not None:
            fu.SetPrefs("Global.UserInterface.AutoClipBrowse", old)
    ld.SetAttrs({"TOOLS_Name": name})
    ld.SetInput("Clip", path)
    for k, v in (("Loop", 1), ("HoldLastFrame", 100000)):
        try:
            ld.SetInput(k, v)
        except Exception:
            pass
    return ld


def add_face_image(comp, o, W, H, col, layer, ttools):
    """面を画像で塗る: 面だけの白い Text+ を型にして、大きさを合わせた画像を重ねる"""
    fc = o["face"]
    im = fc["img"]
    mask = comp.AddTool("TextPlus", col, 20)
    apply_inputs(mask, text_inputs(o, W, H))
    apply_inputs(mask, element_inputs(o, W, H, facemask=True))
    mask.SetAttrs({"TOOLS_Name": f"CT_FaceMask_{o['uid']}"})
    ttools.append(mask)
    if o.get("runs") and o["text"].strip():
        attach_cls(comp, [mask], o, W, col, tag="m", facemask=True)
    ld = _loader(comp, im["file"], col, 21, f"CT_FaceImage_{o['uid']}")
    bg = comp.AddTool("Background", col, 22)
    apply_inputs(bg, {"Width": W, "Height": H, "UseFrameFormatSettings": 0, "TopLeftAlpha": 0.0})
    pic = bg
    for k, (li, r, dest) in enumerate(R.face_img_parts(o)):
        rs = comp.AddTool("BetterResize", col, 23 + k)
        rs.ConnectInput("Input", ld)
        apply_inputs(rs, {"Width": max(1, int(round(dest.width()))), "Height": max(1, int(round(dest.height()))),
                          "KeepAspect": 0})
        mg = comp.AddTool("Merge", col, 24 + k)
        mg.ConnectInput("Background", pic)
        mg.ConnectInput("Foreground", rs)
        cx, cy = dest.center().x() / W, 1 - dest.center().y() / H
        apply_inputs(mg, {"Center": {1: cx, 2: cy, 3: 0}})
        if RL.active(o):
            try:
                mg.Center.SetExpression(RL.fusion_center_expr(o, W, H, cx, cy))
            except Exception:
                pass
        pic = mg
    top = comp.AddTool("Merge", col, 30)
    top.SetAttrs({"TOOLS_Name": f"CT_FaceFill_{o['uid']}"})
    top.ConnectInput("Background", layer)
    top.ConnectInput("Foreground", pic)
    apply_inputs(top, {"Blend": float(fc.get("opacity", 1.0))})
    top.ConnectInput("EffectMask", mask)
    return top


def _face_text(comp, o, W, H, col, row, name, soft=0.0, keep_all=True):
    t = comp.AddTool("TextPlus", col, row)
    apply_inputs(t, text_inputs(o, W, H))
    apply_inputs(t, element_inputs(o, W, H, facemask=(soft if soft > 0 else True)))
    t.SetAttrs({"TOOLS_Name": name})
    if o.get("runs") and o["text"].strip():
        attach_cls(comp, [t], o, W, col, tag=name[3:6], facemask=("all" if keep_all else True))
    return t


def add_emboss(comp, o, W, H, col, layer, ttools):
    """面のエンボス: ぼかした面（Text+ のソフト）を高さにして、プレビューと同じ式で明暗を出す（Custom Tool）"""
    emb = o["face"]["emboss"]
    blur = float(emb.get("blur", 0))
    ang = math.radians(float(emb.get("angle", 225)))
    lx, ly = math.cos(ang), math.sin(ang)
    k = float(emb.get("depth", 1)) * max(1.0, blur) * 2 * R.EMBOSS_K / 2.0
    soft = _face_text(comp, o, W, H, col, 60, f"CT_EmbossSoft_{o['uid']}", soft=blur)
    ttools.append(soft)
    ct = comp.AddTool("Custom", col, 61)
    ct.SetAttrs({"TOOLS_Name": f"CT_Emboss_{o['uid']}"})
    i1 = ("-((geta1d(x+1/w,y)-geta1d(x-1/w,y))*%.6f + (geta1d(x,y-1/h)-geta1d(x,y+1/h))*%.6f)*%.6f" % (lx, ly, k))
    apply_inputs(ct, {"Intermediate1": i1,
                      "RedExpression": "min(1,max(i1,0))", "GreenExpression": "min(1,max(i1,0))",
                      "BlueExpression": "min(1,max(i1,0))", "AlphaExpression": "min(1,abs(i1))"})
    ct.ConnectInput("Image1", soft)
    face = _face_text(comp, o, W, H, col, 62, f"CT_EmbossFace_{o['uid']}")
    ttools.append(face)
    mg = comp.AddTool("Merge", col, 63)
    mg.ConnectInput("Background", layer)
    mg.ConnectInput("Foreground", ct)
    mg.ConnectInput("EffectMask", face)
    return mg


def add_inner_shadow(comp, o, W, H, col, layer, ttools):
    """カットアウトシャドー: 面をずらしてぼかした形の外側を影の色にし、面の中だけに重ねる"""
    ins = o["inner"]
    blur = float(ins.get("blur", 0))
    dist = float(ins.get("distance", 0))
    ang = math.radians(float(ins.get("angle", 45)))
    m1 = _face_text(comp, o, W, H, col, 40, f"CT_InnerSoft_{o['uid']}", soft=blur)
    ttools.append(m1)
    xf = comp.AddTool("Transform", col, 41)
    apply_inputs(xf, {"Center": {1: 0.5 + math.cos(ang) * dist / W, 2: 0.5 - math.sin(ang) * dist / H, 3: 0.0}})
    xf.ConnectInput("Input", m1)
    bg = comp.AddTool("Background", col, 42)
    r, g, b = _rgb(ins.get("color", "#000000"))
    apply_inputs(bg, {"Width": W, "Height": H, "UseFrameFormatSettings": 0,
                      "TopLeftRed": r, "TopLeftGreen": g, "TopLeftBlue": b, "TopLeftAlpha": 1.0})
    bg.ConnectInput("EffectMask", xf)
    apply_inputs(bg, {"ApplyMaskInverted": 1})
    bg.SetAttrs({"TOOLS_Name": f"CT_Inner_{o['uid']}"})
    m2 = _face_text(comp, o, W, H, col, 43, f"CT_InnerFace_{o['uid']}")
    ttools.append(m2)
    mg = comp.AddTool("Merge", col, 44)
    mg.ConnectInput("Background", layer)
    mg.ConnectInput("Foreground", bg)
    apply_inputs(mg, {"Blend": float(ins.get("opacity", 0.6))})
    mg.ConnectInput("EffectMask", m2)
    return mg


def extrude_grad(comp, o, sil, W, H, col):
    """押し出しのグラデーション: 奥行きの違う帯を奥から順に重ね、最後に不透明度を掛ける（プレビューと同じ帯）"""
    ex = o["extrude"]
    a = math.radians(float(ex["angle"]))
    stack = None
    for j, n, qc in reversed(R.extrude_bands(ex)):
        db = comp.AddTool("Fuse.Duplicate", col, 50 + j * 2)
        apply_inputs(db, {"Copies": n + 1,
                          "Center": {1: 0.5 + math.cos(a) / W, 2: 0.5 - math.sin(a) / H, 3: 0.0},
                          "MergeUnder": 1})
        db.ConnectInput("Background", sil)
        bg = comp.AddTool("Background", col, 51 + j * 2)
        apply_inputs(bg, {"Width": W, "Height": H, "UseFrameFormatSettings": 0,
                          "TopLeftRed": qc.redF(), "TopLeftGreen": qc.greenF(), "TopLeftBlue": qc.blueF(),
                          "TopLeftAlpha": 1.0})
        bg.ConnectInput("EffectMask", db)
        if stack is None:
            stack = bg
        else:
            mg = comp.AddTool("Merge", col, 52 + j * 2)
            mg.ConnectInput("Background", stack)
            mg.ConnectInput("Foreground", bg)
            stack = mg
    bc = comp.AddTool("BrightnessContrast", col, 90)
    apply_inputs(bc, {"Gain": float(ex.get("opacity", 1)), "Alpha": 1})
    bc.ConnectInput("Input", stack)
    return bc


def follow_target(doc, o):
    """画像がいっしょに流れるロール／クロールのテロップ（なければ None）"""
    uid = o.get("follow") or ""
    for t in doc["objects"]:
        if uid and t.get("uid") == uid and not R.is_image(t) and RL.active(t):
            return t
    return None


def add_image(comp, doc, o, W, H, col):
    """画像のテロップ: Loader → 透明の画面に Merge（位置・大きさ・回転は Merge で。画面の外でも欠けない）"""
    im = o.get("image") or {}
    fu = FUSION                           # Loader を足すときにファイル選択が開かないように
    old = None
    try:
        if fu is not None:
            old = fu.GetPrefs("Global.UserInterface.AutoClipBrowse")
            fu.SetPrefs("Global.UserInterface.AutoClipBrowse", False)
        ld = comp.AddTool("Loader", col, 1)
    finally:
        if fu is not None and old is not None:
            fu.SetPrefs("Global.UserInterface.AutoClipBrowse", old)
    ld.SetAttrs({"TOOLS_Name": f"CT_Image_{o['uid']}"})
    ld.SetInput("Clip", im.get("file", ""))
    for k, v in (("Loop", 1), ("HoldLastFrame", 100000)):
        try:
            ld.SetInput(k, v)
        except Exception:
            pass
    bg = comp.AddTool("Background", col, 3)
    apply_inputs(bg, {"Width": W, "Height": H, "UseFrameFormatSettings": 0, "TopLeftAlpha": 0.0})
    bg.SetAttrs({"TOOLS_Name": f"CT_ImageBG_{o['uid']}"})
    mg = comp.AddTool("Merge", col, 2)
    mg.SetAttrs({"TOOLS_Name": f"CT_ImageMerge_{o['uid']}"})
    mg.ConnectInput("Background", bg)
    mg.ConnectInput("Foreground", ld)
    r = R.image_rect(o)
    cx, cy = r.center().x() / W, 1 - r.center().y() / H
    xf = o.get("xf") or {}
    sx = float(xf.get("sx", 100) or 100) / 100.0
    apply_inputs(mg, {"Center": {1: cx, 2: cy, 3: 0}, "Size": sx, "Angle": float(xf.get("rot", 0) or 0),
                      "Blend": max(0.0, min(1.0, float(im.get("opacity", 1.0))))})
    tgt = follow_target(doc, o)
    if tgt is not None:
        try:
            mg.Center.SetExpression(RL.fusion_center_expr(tgt, W, H, cx, cy))
        except Exception:
            pass
    return mg


def group_center(doc, render_fn=None):
    """全テロップを合わせた範囲の中心(px)"""
    render_fn = render_fn or R.render
    r = None
    for o in doc["objects"]:
        if not o["text"].strip():
            continue
        info = render_fn(o)[1]
        q = info["text"] if info.get("box") is None else info["text"].united(info["box"])
        r = q if r is None else r.united(q)
    if r is None:
        W, H = doc["canvas"]
        return (W / 2, H / 2)
    return (r.center().x(), r.center().y())


def add_xf(comp, layer, o, W, H, col, row):
    """テロップ自体の回転・拡大（pos を中心に）"""
    xf = o.get("xf") or {}
    rot = float(xf.get("rot", 0) or 0)
    sx = float(xf.get("sx", 100) or 100) / 100.0
    sy = float(xf.get("sy", 100) or 100) / 100.0
    if abs(rot) < 1e-6 and abs(sx - 1) < 1e-6 and abs(sy - 1) < 1e-6:
        return layer
    x, y = o["pos"]
    t = comp.AddTool("Transform", col, row)
    t.SetAttrs({"TOOLS_Name": f"CT_XF_{o.get('uid', '')}"})
    t.ConnectInput("Input", layer)
    apply_inputs(t, {"Pivot": {1: x / W, 2: 1 - y / H, 3: 0},
                     "UseSizeAndAspect": 0, "XSize": sx, "YSize": sy, "Angle": rot})
    return t


def comp_tags(comp):
    """comp 内の Claps タグ一覧 [(gid, uid)]"""
    out = []
    try:
        for t in (comp.GetToolList(False, "TextPlus") or {}).values():
            r = parse_tag(t.GetInput("Comments"))
            if r:
                out.append(r)
    except Exception:
        pass
    return out
