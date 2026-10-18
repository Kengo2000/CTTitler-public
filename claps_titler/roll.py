"""ロール（縦に流す）とクロール（横に流す）の計算。プレビューと Text+ 出力で同じ式を使う
  移動量 d（キャンバスの px。ロールは縦・下が＋、クロールは横・右が＋）を、フレーム f（0〜尺-1）から求める。
  Text+ では comp.RenderStart / RenderEnd を見るので、タイムラインで尺を変えても流れ切るように追従する。"""
import math

from . import render as R

DIR_MODES = [("ロール（下から上へ）", "up"), ("クロール（右から左へ）", "left"), ("クロール（左から右へ）", "right")]
START_MODES = [("画面の外から入る", "below"), ("今の位置から", "here")]
END_MODES = [("画面の外へ抜ける", "above"), ("最後の行を画面中央で止める", "center"), ("今の位置で止める", "here")]
REPEAT_MODES = [("シングル（1回だけ流す）", "single"), ("ループ（くり返し流す）", "loop")]
SPEED_MODES = [("尺に合わせる", "fit"), ("1フレームあたりの速さ", "ppf")]


def end_label(direction, key):
    """終わり方の表示名（ロールとクロールで言い方を変える）"""
    if key == "center":
        return "最後の行を画面中央で止める" if direction == "up" else "画面中央で止める"
    return {k: l for l, k in END_MODES}[key]


def get(o):
    r = dict({"enabled": False, "dir": "up", "start": "below", "end": "above", "speed": "fit", "ppf": 4,
              "snap": True, "repeat": "single"}, **(o.get("roll") or {}))
    if r["dir"] not in ("up", "left", "right"):
        r["dir"] = "up"
    if r.get("repeat") == "loop":                  # ループは「外から入って外へ抜ける」をくり返す
        r["start"], r["end"] = "below", "above"
    return r


def looping(o):
    return get(o).get("repeat") == "loop"


def active(o):
    return bool(get(o).get("enabled")) and bool((o.get("text") or "").strip())


def horizontal(o):
    return get(o)["dir"] in ("left", "right")


def _margin(o):
    """画面の外に完全に出すための余白（縁・影・押し出しの分）"""
    m = sum(float(e.get("width", 0)) for e in o["edges"] if e.get("enabled"))
    for a in R.char_styles(o):                     # 文字ごとに太くした縁も
        for v in ((a or {}).get("ew") or {}).values():
            m = max(m, float(v or 0))
    sh = o.get("shadow") or {}
    if sh.get("enabled"):
        m += float(sh.get("distance", 0)) + float(sh.get("blur", 0)) * 3
    ex = o.get("extrude") or {}
    if ex.get("enabled"):
        m += float(ex.get("depth", 0))
    bx = o.get("box") or {}
    if bx.get("enabled") and bx.get("mode", "fit") == "fit":
        m += max(float(bx.get("padx", 0)), float(bx.get("pady", 0)))
    return m + 8


def plan(o, W, H):
    """-> (d0, d1)  始まりと終わりの移動量（px）。ロールは縦（下が＋）、クロールは横（右が＋）"""
    r = get(o)
    b = R.block_rect(o)
    m = _margin(o)
    if r["dir"] == "up":
        d0 = (H + m - b.top()) if r["start"] == "below" else 0.0
        if r["end"] == "above":
            d1 = -(b.bottom() + m)
        elif r["end"] == "center":
            L = R.text_layout(o)
            lp = L["line_paths"][-1].boundingRect() if L["line_paths"] else b
            last_c = lp.center().y() if lp.height() > 0 else b.bottom()
            d1 = H / 2.0 - last_c
        else:
            d1 = 0.0
        d1 = min(d1, d0)                            # 下へは流さない
    elif r["dir"] == "left":
        d0 = (W + m - b.left()) if r["start"] == "below" else 0.0
        if r["end"] == "above":
            d1 = -(b.right() + m)
        elif r["end"] == "center":
            d1 = W / 2.0 - b.center().x()
        else:
            d1 = 0.0
        d1 = min(d1, d0)                            # 右へは戻さない
    else:                                           # right: 左から右へ
        d0 = -(b.right() + m) if r["start"] == "below" else 0.0
        if r["end"] == "above":
            d1 = W + m - b.left()
        elif r["end"] == "center":
            d1 = W / 2.0 - b.center().x()
        else:
            d1 = 0.0
        d1 = max(d1, d0)
    return round(d0), round(d1)


def speed(o, W, H, total):
    """1フレームあたりの移動量(px) と、流れ切るのに必要なフレーム数"""
    r = get(o)
    d0, d1 = plan(o, W, H)
    dist = abs(d1 - d0)
    n = max(1, total - 1)
    if r["speed"] == "ppf":
        v = max(1.0, float(r["ppf"]))
    else:
        v = dist / n
        if r["snap"]:
            v = max(1.0, math.ceil(v - 1e-9))     # 切り上げ: 尺の中で必ず流れ切る（少し早く着く）
    need = int(math.ceil(dist / v)) + 1 if v > 0 else 1
    return v, need


def d_at(o, frame, total, W, H):
    d0, d1 = plan(o, W, H)
    v, _ = speed(o, W, H, total)
    s = 1 if d1 >= d0 else -1
    dist = abs(d1 - d0)
    if looping(o) and dist > 0:
        x = frame * v
        return d0 + s * (x - dist * math.floor(x / dist))
    return d0 + s * min(dist, frame * v)


def fusion_expr(o, W, H):
    """Fusion の式: 今の時間での移動量（px）"""
    r = get(o)
    d0, d1 = plan(o, W, H)
    dist = abs(d1 - d0)
    s = "+" if d1 >= d0 else "-"
    f = "(time - comp.RenderStart)"
    n = "max(1, comp.RenderEnd - comp.RenderStart)"
    if r["speed"] == "ppf":
        v = f"{max(1.0, float(r['ppf'])):.4f}"
    elif r["snap"]:
        v = f"max(1, -floor(-{dist} / {n} + 0.000000001))"
    else:
        v = f"({dist} / {n})"
    if looping(o) and dist > 0:
        x = f"({f} * {v})"
        return f"({d0} {s} ({x} - {dist} * floor({x} / {dist})))"
    return f"({d0} {s} min({dist}, {f} * {v}))"


def fusion_center_expr(o, W, H, cx, cy):
    """Text+ の Center の式（cx, cy は流す前の Center）"""
    ex = fusion_expr(o, W, H)
    if horizontal(o):
        return f"Point({cx:.6f} + {ex} / {W}, {cy:.6f})"
    return f"Point({cx:.6f}, {cy:.6f} - {ex} / {H})"
