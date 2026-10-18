"""簡易イン/アウトアニメーション

・Fusion 側: Transform / Blur / Merge(Blend) をエクスプレッションで動かす。
  comp.RenderStart / RenderEnd を見るので、タイムラインで尺を変えても自動で追従する。
・プレビュー側: 同じ式を Python で計算して再現する。
"""
import math

MODES = [
    ("なし", "none"),
    ("左から右へ（slide_right）", "slide_right"),
    ("右から左へ（slide_left）", "slide_left"),
    ("上から下へ（slide_down）", "slide_down"),
    ("下から上へ（slide_up）", "slide_up"),
    ("ズームイン（zoom_in）", "zoom_in"),
    ("ズームアウト（zoom_out）", "zoom_out"),
    ("ぼかし（blur）", "blur"),
    ("ストレッチ（stretch）", "stretch"),
    ("フェード（fade）", "fade"),
    ("アースクエイク（earthquake）", "quake"),
]
EASES = [("リニア", "linear"), ("なめらか(Quad)", "quad"), ("なめらか(Cubic)", "cubic"),
         ("強め(Quart)", "quart"), ("急(Expo)", "expo"), ("行き過ぎ(Back)", "back"),
         ("弾む(Elastic)", "elastic"), ("バウンド(Bounce)", "bounce")]

BLUR_K = 1.0     # こちらのぼかし(px) → Fusion Blur の Size

DEFAULT = {"in": "none", "out": "none", "dur": 0.5, "ease_in": "cubic", "ease_out": "cubic",
           "zoom": 2.0, "blur": 30.0, "motion_blur": False, "pivot_mode": "auto", "pivot": [960, 540],
           "quake_amp": 25.0, "quake_speed": 14.0, "quake_rot": 2.0, "quake_hold": False}
PIVOT_MODES = [("テロップの中心（まとめて＝全体の中心）", "auto"), ("画面の中心", "screen"), ("指定した位置", "custom")]


def effective(doc, o, group_center):
    """実際に使うアニメ設定と拡大の中心(px)。まとめアニメがオンならそちらを全テロップ共通で使う
    -> ({"anim": {...}, "uid": ...}, (cx, cy))"""
    W, H = doc["canvas"]
    ga = doc.get("ganim") or {}
    if ga.get("enabled"):
        a = dict(DEFAULT)
        a.update(ga)
        pm = a.get("pivot_mode", "auto")
        auto = group_center or (W / 2, H / 2)
    else:
        a = get(o)
        pm = a.get("pivot_mode", "auto")
        auto = tuple(o["pos"])
    if pm == "screen":
        c = (W / 2, H / 2)
    elif pm == "custom":
        c = tuple(a.get("pivot") or (W / 2, H / 2))
    else:
        c = auto
    return {"anim": a, "uid": o.get("uid", "")}, c


def get(o):
    a = dict(DEFAULT)
    a.update(o.get("anim") or {})
    return a


def active(o):
    a = get(o)
    return a["in"] != "none" or a["out"] != "none" or bool(a.get("quake_hold"))


# ---------------- アースクエイク（揺れ）
# 周波数の違う sin を3つ重ねて、不規則に見える揺れを作る（毎回同じ揺れ方になる）
_QX = ((1.00, 0.0, 0.55), (1.73, 1.7, 0.30), (2.91, 4.1, 0.15))
_QY = ((1.21, 2.3, 0.55), (2.07, 0.6, 0.30), (3.37, 5.2, 0.15))
_QR = ((0.83, 3.1, 0.60), (1.91, 1.1, 0.40))


def _noise_py(tab, sec, hz):
    return sum(w * math.sin(2 * math.pi * hz * m * sec + ph) for m, ph, w in tab)


def _noise_lua(tab, sec, hz):
    return "(" + " + ".join(f"{w} * math.sin({2 * math.pi * hz * m:.5f} * {sec} + {ph})" for m, ph, w in tab) + ")"


def _quake_amount(a, kin, kout):
    """揺れの強さ 0〜1（イン/アウトのアースクエイク分、ずっと揺らすなら 1）"""
    if a.get("quake_hold"):
        return 1.0
    q = 0.0
    if a["in"] == "quake":
        q += kin
    if a["out"] == "quake":
        q += kout
    return min(1.0, q)


# ---------------- 緩急（0→1 を 0→1 に。終わりがゆっくりになる「アウト」型）
def ease_py(kind, x):
    x = max(0.0, min(1.0, x))
    if kind == "linear":
        return x
    if kind == "quad":
        return 1 - (1 - x) ** 2
    if kind == "cubic":
        return 1 - (1 - x) ** 3
    if kind == "quart":
        return 1 - (1 - x) ** 4
    if kind == "expo":
        return 1.0 if x >= 1 else 1 - 2 ** (-10 * x)
    if kind == "back":
        return 1 + 2.70158 * (x - 1) ** 3 + 1.70158 * (x - 1) ** 2
    if kind == "elastic":
        if x <= 0 or x >= 1:
            return x
        return 2 ** (-10 * x) * math.sin((x * 10 - 0.75) * (2 * math.pi / 3)) + 1
    if kind == "bounce":
        n1, d1 = 7.5625, 2.75
        if x < 1 / d1:
            return n1 * x * x
        if x < 2 / d1:
            x -= 1.5 / d1
            return n1 * x * x + 0.75
        if x < 2.5 / d1:
            x -= 2.25 / d1
            return n1 * x * x + 0.9375
        x -= 2.625 / d1
        return n1 * x * x + 0.984375
    return x


def ease_lua(kind, x):
    """Fusion エクスプレッション（Lua）版。x は式の文字列"""
    x = f"math.min(1, math.max(0, {x}))"
    if kind == "linear":
        return x
    if kind == "quad":
        return f"(1 - (1 - {x})^2)"
    if kind == "cubic":
        return f"(1 - (1 - {x})^3)"
    if kind == "quart":
        return f"(1 - (1 - {x})^4)"
    if kind == "expo":
        return f"iif({x} >= 1, 1, 1 - 2^(-10 * {x}))"
    if kind == "back":
        return f"(1 + 2.70158 * ({x} - 1)^3 + 1.70158 * ({x} - 1)^2)"
    if kind == "elastic":
        return (f"iif({x} <= 0, 0, iif({x} >= 1, 1, "
                f"2^(-10 * {x}) * math.sin(({x} * 10 - 0.75) * 2.0943951) + 1))")
    if kind == "bounce":
        return (f"iif({x} < 0.363636, 7.5625 * {x}^2, "
                f"iif({x} < 0.727273, 7.5625 * ({x} - 0.545455)^2 + 0.75, "
                f"iif({x} < 0.909091, 7.5625 * ({x} - 0.818182)^2 + 0.9375, "
                f"7.5625 * ({x} - 0.954545)^2 + 0.984375)))")
    return x


# ---------------- 各モードの効果（e: 0=画面外/消えている → 1=定位置）
def _effect(mode, e, side, a):
    """side: "in" or "out"。戻り値 dict(dx, dy, sx, sy, op, blur)  dx,dy は画面幅/高さ比"""
    r = {"dx": 0.0, "dy": 0.0, "sx": 1.0, "sy": 1.0, "op": 1.0, "blur": 0.0}
    k = 1.0 - e
    sign = -1.0 if side == "in" else 1.0     # イン: 手前から来る / アウト: 進行方向へ去る
    if mode == "slide_right":
        r["dx"] = sign * k
    elif mode == "slide_left":
        r["dx"] = -sign * k
    elif mode == "slide_down":
        r["dy"] = sign * k
    elif mode == "slide_up":
        r["dy"] = -sign * k
    elif mode == "zoom_in":
        s = e if side == "in" else 1 + (a["zoom"] - 1) * k
        r["sx"] = r["sy"] = s
        if side == "out":
            r["op"] = e
    elif mode == "zoom_out":
        s = 1 + (a["zoom"] - 1) * k if side == "in" else e
        r["sx"] = r["sy"] = s
        if side == "in":
            r["op"] = e
    elif mode == "blur":
        r["blur"] = a["blur"] * k
        r["op"] = e
    elif mode == "stretch":
        r["sx"] = e
        r["sy"] = 1 + 0.6 * k
    elif mode == "fade":
        r["op"] = e
    elif mode == "quake" and side == "out":
        r["op"] = min(1.0, 3 * e)          # 揺れながら、最後の 1/3 で消える
    return r


def compute(o, frame, total, fps):
    """プレビュー用: frame(0〜total-1) での変形量"""
    a = get(o)
    d = max(1.0, a["dur"] * fps)
    ein = ease_py(a["ease_in"], frame / d) if a["in"] != "none" else 1.0
    eout = ease_py(a["ease_out"], (total - 1 - frame) / d) if a["out"] != "none" else 1.0
    ri = _effect(a["in"], ein, "in", a)
    ro = _effect(a["out"], eout, "out", a)
    # 揺れは緩急をかけず直線で弱まる/強まる
    q = _quake_amount(a, 1 - min(1.0, frame / d), 1 - min(1.0, (total - 1 - frame) / d))
    sec = frame / fps
    hz = float(a["quake_speed"])
    return {"dx": ri["dx"] + ro["dx"], "dy": ri["dy"] + ro["dy"],
            "sx": ri["sx"] * ro["sx"], "sy": ri["sy"] * ro["sy"],
            "op": ri["op"] * ro["op"], "blur": ri["blur"] + ro["blur"],
            "dxp": q * float(a["quake_amp"]) * _noise_py(_QX, sec, hz),
            "dyp": q * float(a["quake_amp"]) * _noise_py(_QY, sec, hz),
            "rot": q * float(a["quake_rot"]) * _noise_py(_QR, sec, hz)}


# ---------------- Fusion 用の式
def _side_exprs(mode, e, side, a):
    """モードごとの (dx, dy, sx, sy, op, blur) の式（文字列）"""
    k = f"(1 - {e})"
    sign = "-1" if side == "in" else "1"
    z = float(a["zoom"])
    ex = {"dx": "0", "dy": "0", "sx": "1", "sy": "1", "op": "1", "blur": "0"}
    if mode == "slide_right":
        ex["dx"] = f"({sign} * {k})"
    elif mode == "slide_left":
        ex["dx"] = f"(-{sign} * {k})"
    elif mode == "slide_down":
        ex["dy"] = f"({sign} * {k})"
    elif mode == "slide_up":
        ex["dy"] = f"(-{sign} * {k})"
    elif mode == "zoom_in":
        ex["sx"] = ex["sy"] = e if side == "in" else f"(1 + {z - 1} * {k})"
        if side == "out":
            ex["op"] = e
    elif mode == "zoom_out":
        ex["sx"] = ex["sy"] = f"(1 + {z - 1} * {k})" if side == "in" else e
        if side == "in":
            ex["op"] = e
    elif mode == "blur":
        ex["blur"] = f"({float(a['blur'])} * {k})"
        ex["op"] = e
    elif mode == "stretch":
        ex["sx"] = e
        ex["sy"] = f"(1 + 0.6 * {k})"
    elif mode == "fade":
        ex["op"] = e
    elif mode == "quake" and side == "out":
        ex["op"] = f"math.min(1, 3 * {e})"
    return ex


def fusion_exprs(o, fps):
    a = get(o)
    d = max(1.0, a["dur"] * fps)
    pin = f"((time - comp.RenderStart) / {d:.4f})"
    pout = f"((comp.RenderEnd - time) / {d:.4f})"
    ein = ease_lua(a["ease_in"], pin) if a["in"] != "none" else "1"
    eout = ease_lua(a["ease_out"], pout) if a["out"] != "none" else "1"
    i = _side_exprs(a["in"], ein, "in", a)
    o_ = _side_exprs(a["out"], eout, "out", a)
    r = {"dx": f"({i['dx']} + {o_['dx']})", "dy": f"({i['dy']} + {o_['dy']})",
         "sx": f"({i['sx']} * {o_['sx']})", "sy": f"({i['sy']} * {o_['sy']})",
         "op": f"({i['op']} * {o_['op']})", "blur": f"({i['blur']} + {o_['blur']})",
         "dxp": "0", "dyp": "0", "rot": "0"}
    if a.get("quake_hold") or "quake" in (a["in"], a["out"]):
        if a.get("quake_hold"):
            q = "1"
        else:
            parts = []
            if a["in"] == "quake":
                parts.append(f"(1 - math.min(1, {pin}))")
            if a["out"] == "quake":
                parts.append(f"(1 - math.min(1, math.max(0, {pout})))")
            q = f"math.min(1, math.max(0, {' + '.join(parts)}))"
        sec = f"((time - comp.RenderStart) / {fps:.5f})"
        hz = float(a["quake_speed"])
        r["dxp"] = f"({q} * {float(a['quake_amp'])} * {_noise_lua(_QX, sec, hz)})"
        r["dyp"] = f"({q} * {float(a['quake_amp'])} * {_noise_lua(_QY, sec, hz)})"
        r["rot"] = f"({q} * {float(a['quake_rot'])} * {_noise_lua(_QR, sec, hz)})"
    return r


def fusion_add(comp, layer, o, W, H, fps, center, col=0, row=10):
    """layer の後ろにアニメ用ノードを足して、最後のノードを返す。center=(x,y) は拡大の中心(px)"""
    a = get(o)
    if not active(o):
        return layer
    ex = fusion_exprs(o, fps)
    uid = o.get("uid", "")
    cx, cy = center[0] / W, 1 - center[1] / H
    tr = comp.AddTool("Transform", col, row)
    tr.SetAttrs({"TOOLS_Name": f"CT_Anim_{uid}"})
    tr.ConnectInput("Input", layer)
    tr.SetInput("Pivot", {1: cx, 2: cy, 3: 0})
    tr.Center.SetExpression(f"Point(0.5 + {ex['dx']} + {ex['dxp']} / {W}, 0.5 - ({ex['dy']} + {ex['dyp']} / {H}))")
    if ex["rot"] != "0":
        tr.Angle.SetExpression(f"-({ex['rot']})")     # こちらは＋が時計回り、Fusion は＋が反時計回り
    tr.XSize.SetExpression(ex["sx"])
    tr.YSize.SetExpression(ex["sy"])
    try:
        tr.SetInput("UseSizeAndAspect", 0)
    except Exception:
        pass
    if a.get("motion_blur"):
        try:
            tr.SetInput("MotionBlur", 1)
            tr.SetInput("Quality", 4)
        except Exception:
            pass
    out = tr
    if "blur" in (a["in"], a["out"]):
        bl = comp.AddTool("Blur", col, row + 1)
        bl.SetAttrs({"TOOLS_Name": f"CT_AnimBlur_{uid}"})
        bl.ConnectInput("Input", out)
        bl.XBlurSize.SetExpression(f"{ex['blur']} * {BLUR_K}")
        out = bl
    if ex["op"] != "(1 * 1)":
        bg = comp.AddTool("Background", col, row + 2)
        bg.SetAttrs({"TOOLS_Name": f"CT_AnimBG_{uid}"})
        for k, v in {"Width": W, "Height": H, "UseFrameFormatSettings": 0, "TopLeftAlpha": 0}.items():
            bg.SetInput(k, v)
        mg = comp.AddTool("Merge", col, row + 3)
        mg.SetAttrs({"TOOLS_Name": f"CT_AnimFade_{uid}"})
        mg.ConnectInput("Background", bg)
        mg.ConnectInput("Foreground", out)
        mg.Blend.SetExpression(ex["op"])
        out = mg
    return out
