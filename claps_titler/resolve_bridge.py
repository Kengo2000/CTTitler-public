"""DaVinci Resolve との連携（外部スクリプトAPI）"""
import os
import re
import sys

from .paths import IS_WIN, RESOLVE_API, RESOLVE_DIR, RESOLVE_LIB

BIN_NAME = "CT Titles"
OLD_BIN_NAMES = ("Claps Titles",)   # 旧名のビンもそのまま使う
# PNGファイル名: <グループID>[_o<テロップID>]_r<版>_CT.png
# （末尾を文字にして連番素材と誤認されないようにする）
PNG_RE = re.compile(r"^(?P<gid>.+?)(?:_o(?P<uid>[0-9a-f]{5}))?_r(?P<rev>\d+)_CT\.png$")


def png_name(gid, rev, uid=None):
    return f"{gid}_o{uid}_r{rev}_CT.png" if uid else f"{gid}_r{rev}_CT.png"


COMPOUND_PREFIX = "CT_"


def gid_of_name(name):
    """複合クリップ名(CT_<gid>)からグループIDを得る"""
    if name and name.startswith(COMPOUND_PREFIX):
        return name[len(COMPOUND_PREFIX):]
    return None


def parse_png(path):
    """-> (gid, uid or None, rev) / 該当しなければ None"""
    m = PNG_RE.match(os.path.basename(path or ""))
    if not m:
        return None
    return m.group("gid"), m.group("uid"), int(m.group("rev"))


def tc_to_frame(tc, fps):
    """タイムコード(ドロップフレーム対応)を絶対フレーム数に"""
    drop = ";" in tc or "," in tc
    h, m, s, f = [int(x) for x in re.split(r"[:;,.]", tc)]
    nominal = int(round(fps))
    frames = ((h * 3600 + m * 60 + s) * nominal) + f
    if drop:
        d = 2 if nominal <= 30 else 4
        total_min = h * 60 + m
        frames -= d * (total_min - total_min // 10)
    return frames


def frame_to_tc(frames, fps, drop):
    nominal = int(round(fps))
    if drop:
        d = 2 if nominal <= 30 else 4
        per10 = nominal * 600 - d * 9
        per1 = nominal * 60 - d
        tens, rem = divmod(frames, per10)
        if rem > d:
            frames += d * 9 * tens + d * ((rem - d) // per1)
        else:
            frames += d * 9 * tens
    f = frames % nominal
    s = (frames // nominal) % 60
    m = (frames // (nominal * 60)) % 60
    h = frames // (nominal * 3600)
    sep = ";" if drop else ":"
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"


class ResolveError(Exception):
    pass


def item_tags(it):
    """Text+ 出力のタイトルなら、その中の (gid, uid) の一覧"""
    try:
        n = it.GetFusionCompCount()
    except Exception:
        return []
    if not n:
        return []
    from .textplus_out import comp_tags
    out = []
    for i in range(1, n + 1):
        try:
            out += comp_tags(it.GetFusionCompByIndex(i))
        except Exception:
            pass
    return out


def item_gid(it):
    """タイムラインアイテムが Claps タイトル（PNG / 複合クリップ / Text+出力）ならグループIDを返す"""
    try:
        g = gid_of_name(it.GetName())
        if g:
            return g
        mpi = it.GetMediaPoolItem()
        if mpi:
            g = gid_of_name(mpi.GetName())
            if g:
                return g
            r = parse_png(mpi.GetClipProperty("File Path"))
            if r:
                return r[0]
        tags = item_tags(it)
        if tags:
            return tags[0][0]
    except Exception:
        pass
    return None


class Bridge:
    def __init__(self):
        self.resolve = None
        self.error = ""

    # ---- 接続 ----
    def connect(self):
        if self.resolve is not None:
            try:
                self.resolve.GetProductName()
                return True
            except Exception:
                self.resolve = None
        os.environ.setdefault("RESOLVE_SCRIPT_API", RESOLVE_API)
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", RESOLVE_LIB)
        mod = os.path.join(RESOLVE_API, "Modules")
        if mod not in sys.path:
            sys.path.append(mod)
        if IS_WIN and hasattr(os, "add_dll_directory") and os.path.isdir(RESOLVE_DIR):
            try:                                   # fusionscript.dll が依存する DLL を見つけられるように
                os.add_dll_directory(RESOLVE_DIR)
            except OSError:
                pass
        try:
            import DaVinciResolveScript as dvr  # noqa
            self.resolve = dvr.scriptapp("Resolve")
            try:
                from . import textplus_out
                textplus_out.FUSION = self.resolve.Fusion()
            except Exception:
                pass
        except Exception as e:  # pragma: no cover
            self.error = f"{type(e).__name__}: {e}"
            self.resolve = None
        if self.resolve is None and not self.error:
            self.error = ("Resolveに接続できません。Resolveが起動しているか、"
                          "環境設定 → システム → 一般 →「外部スクリプト使用」が「ローカル」か確認してください。")
        return self.resolve is not None

    def _ctx(self):
        if not self.connect():
            raise ResolveError(self.error)
        pm = self.resolve.GetProjectManager()
        proj = pm.GetCurrentProject()
        if proj is None:
            raise ResolveError("プロジェクトが開かれていません")
        tl = proj.GetCurrentTimeline()
        return proj, tl, proj.GetMediaPool()

    # ---- 情報 ----
    def info(self):
        proj, tl, _ = self._ctx()
        d = {"project": proj.GetName(), "timeline": None, "width": 1920, "height": 1080, "fps": 29.97}
        if tl:
            d["timeline"] = tl.GetName()
            try:
                d["width"] = int(tl.GetSetting("timelineResolutionWidth"))
                d["height"] = int(tl.GetSetting("timelineResolutionHeight"))
                d["fps"] = float(tl.GetSetting("timelineFrameRate"))
            except Exception:
                pass
        return d

    # ---- 背景 ----
    def _items_at_playhead(self, tl, gid):
        """再生ヘッド位置にある、指定グループのタイトルクリップ"""
        fps = float(tl.GetSetting("timelineFrameRate"))
        f = tc_to_frame(tl.GetCurrentTimecode(), fps)
        found = []
        for idx in range(1, tl.GetTrackCount("video") + 1):
            for it in tl.GetItemListInTrack("video", idx) or []:
                if it.GetStart() <= f < it.GetEnd() and item_gid(it) == gid:
                    found.append(it)
        return found

    def grab_frame(self, path, hide_gid=None):
        """現在フレームを書き出す。hide_gid のタイトルは一時的に隠して背景だけにする"""
        proj, tl, _ = self._ctx()
        if tl is None:
            raise ResolveError("タイムラインが開かれていません")
        hidden = []
        if hide_gid:
            try:
                for it in self._items_at_playhead(tl, hide_gid):
                    if it.GetClipEnabled():
                        it.SetClipEnabled(False)
                        hidden.append(it)
            except Exception:
                pass
        try:
            if os.path.exists(path):
                os.remove(path)
            ok = proj.ExportCurrentFrameAsStill(path)
        finally:
            for it in hidden:
                it.SetClipEnabled(True)
        if not ok or not os.path.exists(path):
            raise ResolveError("現在のフレームを書き出せませんでした（エディットページで再生ヘッドがクリップ上にあるか確認してください）")
        return path

    # ---- 配置 ----
    def _bin(self, mp):
        root = mp.GetRootFolder()
        for f in root.GetSubFolderList() or []:
            if f.GetName() == BIN_NAME or f.GetName() in OLD_BIN_NAMES:
                return f
        return mp.AddSubFolder(root, BIN_NAME)

    def _video_top(self, tl, start, end):
        """その時間に映像（CT Titler のもの以外）が載っている一番上のトラック番号（なければ 0）"""
        top = 0
        for idx in range(1, tl.GetTrackCount("video") + 1):
            for it in tl.GetItemListInTrack("video", idx) or []:
                if it.GetStart() < end and it.GetEnd() > start:
                    try:
                        mine = item_gid(it) is not None
                    except Exception:
                        mine = False
                    if not mine:
                        top = idx
                        break
        return max(top, 1)          # 映像がない所でも V1 には置かない

    def _track_free(self, tl, idx, start, end):
        for it in tl.GetItemListInTrack("video", idx) or []:
            if it.GetStart() < end and it.GetEnd() > start:
                return False
        return True

    def place(self, png_path, seconds, track=0, rec=None, dur=None):
        """再生ヘッド位置(rec指定時はそのフレーム)にPNGを配置。track=0なら空いている一番上(なければトラック追加)"""
        proj, tl, mp = self._ctx()
        if tl is None:
            raise ResolveError("タイムラインが開かれていません")
        fps = float(tl.GetSetting("timelineFrameRate"))
        tc = tl.GetCurrentTimecode()
        if rec is None:
            rec = tc_to_frame(tc, fps)
        if dur is None:
            dur = max(1, int(round(seconds * fps)))

        prev = mp.GetCurrentFolder()
        mp.SetCurrentFolder(self._bin(mp))
        items = mp.ImportMedia([png_path])
        if prev:
            mp.SetCurrentFolder(prev)
        if not items:
            raise ResolveError("PNGをメディアプールに読み込めませんでした")
        mpi = items[0]
        # 静止画は In/Out を打たないと尺指定が無視される
        try:
            mpi.SetMarkInOut(0, dur - 1)
        except Exception:
            pass

        n = tl.GetTrackCount("video")
        idx = None
        if track and 1 <= track <= n:
            idx = track
        else:
            # 映像の直上から上へ、その時間が空いている既存トラックを探す（なければトラック追加）
            for i in range(self._video_top(tl, rec, rec + dur) + 1, n + 1):
                if self._track_free(tl, i, rec, rec + dur):
                    idx = i
                    break
        if idx is None:
            tl.AddTrack("video")
            idx = tl.GetTrackCount("video")

        res = mp.AppendToTimeline([{
            "mediaPoolItem": mpi, "startFrame": 0, "endFrame": dur,
            "trackIndex": idx, "recordFrame": rec, "mediaType": 1,
        }])
        if not res:
            raise ResolveError("タイムラインに配置できませんでした")
        try:
            tl.SetCurrentTimecode(tc)   # 再生ヘッドを元の位置に戻す
        except Exception:
            pass
        return mpi, res[0]

    def place_many(self, pngs, seconds, track=0, compound_name=None, rec=None, dur=None):
        """複数PNGを再生ヘッド位置に下から順に重ねて配置。compound_name があれば複合クリップにまとめる
        -> (mpis, compound_item or None)"""
        proj, tl, mp = self._ctx()
        tc = tl.GetCurrentTimecode()
        n_before = tl.GetTrackCount("video")
        mpis, items = [], []
        for i, png in enumerate(pngs):
            tl.SetCurrentTimecode(tc)
            mpi, item = self.place(png, seconds, track if i == 0 else 0, rec, dur)
            mpis.append(mpi)
            items.append(item)
        comp = None
        if compound_name and items:
            prev = mp.GetCurrentFolder()
            mp.SetCurrentFolder(self._bin(mp))
            try:
                comp = tl.CreateCompoundClip(items, {"name": compound_name})
            finally:
                if prev:
                    mp.SetCurrentFolder(prev)
            if not comp:
                raise ResolveError("複合クリップを作成できませんでした（個別クリップのまま配置されています）")
            # 重ねるために追加したトラックが空になっていたら片付ける
            for idx in range(tl.GetTrackCount("video"), n_before, -1):
                if not (tl.GetItemListInTrack("video", idx) or []):
                    tl.DeleteTrack("video", idx)
        try:
            tl.SetCurrentTimecode(tc)
        except Exception:
            pass
        return mpis, comp

    # ---- 編集に戻る ----
    def selected_title(self):
        """選択中(なければ再生ヘッド下)のClapsタイトルのグループIDを返す"""
        _, tl, _ = self._ctx()
        if tl is None:
            raise ResolveError("タイムラインが開かれていません")
        cands = []
        try:
            cands += tl.GetSelectedClips() or []
        except Exception:
            pass
        for it in cands:
            g = item_gid(it)
            if g:
                return g
        # 選択がなければ、再生ヘッド位置にある Claps タイトルを上のトラックから探す
        try:
            fps = float(tl.GetSetting("timelineFrameRate"))
            f = tc_to_frame(tl.GetCurrentTimecode(), fps)
            for idx in range(tl.GetTrackCount("video"), 0, -1):
                for it in tl.GetItemListInTrack("video", idx) or []:
                    if it.GetStart() <= f < it.GetEnd():
                        g = item_gid(it)
                        if g:
                            return g
        except Exception:
            pass
        raise ResolveError("CT Titlerで作ったタイトルが見つかりません。\n"
                           "タイムラインでタイトルをクリックして選ぶか、再生ヘッドをタイトルの上に置いてから実行してください")

    def find_items_by_gid(self, gid):
        """現在のタイムラインにある、指定グループのクリップ（PNG/Text+どちらでも）"""
        _, tl, _ = self._ctx()
        out = []
        for idx in range(1, tl.GetTrackCount("video") + 1):
            for it in tl.GetItemListInTrack("video", idx) or []:
                if item_gid(it) == gid:
                    out.append(it)
        return out

    def peek_selected(self):
        """監視用: 選択中のClapsタイトルのグループIDを返す（なければNone、例外を出さない）"""
        try:
            if self.resolve is None:
                return None
            proj = self.resolve.GetProjectManager().GetCurrentProject()
            tl = proj.GetCurrentTimeline() if proj else None
            if tl is None:
                return None
            for it in tl.GetSelectedClips() or []:
                g = item_gid(it)
                if g:
                    return g
        except Exception:
            return None
        return None

    def read_textplus(self, element_keys, tool_keys):
        """選択中(なければ再生ヘッド下)の Fusion タイトルから Text+ の設定を読む（読むだけで変更しない）
        -> (タイムライン名, [ {...Text+の値, "elements": [...], "clip": {...}} ])"""
        _, tl, _ = self._ctx()
        if tl is None:
            raise ResolveError("タイムラインが開かれていません")
        cands = []
        try:
            cands += tl.GetSelectedClips() or []
        except Exception:
            pass
        if not cands:
            fps = float(tl.GetSetting("timelineFrameRate"))
            f = tc_to_frame(tl.GetCurrentTimecode(), fps)
            for idx in range(tl.GetTrackCount("video"), 0, -1):
                for it in tl.GetItemListInTrack("video", idx) or []:
                    if it.GetStart() <= f < it.GetEnd() and it.GetFusionCompCount() > 0:
                        cands.append(it)
        out = []
        for it in cands:
            try:
                n = it.GetFusionCompCount()
            except Exception:
                n = 0
            if not n:
                continue
            comp = it.GetFusionCompByIndex(1)
            tools = comp.GetToolList(False, "TextPlus") or {}
            ttype, tindex = it.GetTrackTypeAndIndex()
            clip = {"name": it.GetName(), "start": it.GetStart(), "dur": it.GetDuration(), "track": tindex}
            for tool in tools.values():
                if tool.GetAttrs().get("TOOLB_PassThrough"):
                    continue
                d = {"clip": clip, "tool": tool.GetAttrs().get("TOOLS_Name")}
                for k in tool_keys:
                    d[k] = tool.GetInput(k)
                elems = []
                for i in range(1, 9):
                    e = {k: tool.GetInput(f"{k}{i}") for k in element_keys}
                    if not e.get("Enabled"):
                        continue
                    g = tool.GetInput(f"ShadingGradient{i}")
                    try:
                        e["gradient"] = g.Value if g is not None else None
                    except Exception:
                        e["gradient"] = None
                    elems.append(e)
                d["elements"] = elems
                out.append(d)
        if not out:
            raise ResolveError("Text+ を含むタイトルが選択されていません（タイムラインでクリックしてから実行してください）")
        return tl.GetName(), out

    # ---- Text+ 出力 ----
    def _refresh(self, tl, comp=None):
        """外部から Fusion を書き換えた後、ビューアを更新させる"""
        try:
            if comp is not None:
                for t in (comp.GetToolList(False, "TextPlus") or {}).values():
                    s = t.GetInput("StyledText")
                    t.SetInput("StyledText", (s or "") + " ")
                    t.SetInput("StyledText", s or "")
                    break
            tc = tl.GetCurrentTimecode()
            fps = float(tl.GetSetting("timelineFrameRate"))
            f = tc_to_frame(tc, fps)
            tl.SetCurrentTimecode(frame_to_tc(f + 1, fps, ";" in tc))
            tl.SetCurrentTimecode(tc)
        except Exception:
            pass

    def place_textplus(self, placeholder, groups, seconds, track, builder,
                       compound_name=None, rec=None, dur=None):
        """透明な下地クリップを置き、その Fusion に Text+ を組む。
        groups: [uid or None, ...]（None=全テロップを1つに） builder(comp, uid) で中身を作る
        -> ({uid: item}, compound_item)"""
        proj, tl, mp = self._ctx()
        tc = tl.GetCurrentTimecode()
        n_before = tl.GetTrackCount("video")
        items = {}
        for i, uid in enumerate(groups):
            tl.SetCurrentTimecode(tc)
            _, item = self.place(placeholder, seconds, track if i == 0 else 0, rec, dur)
            comp = item.AddFusionComp()
            builder(comp, uid)
            self._refresh(tl, comp)
            items[uid] = item
        comp_item = None
        if compound_name and items:
            prev = mp.GetCurrentFolder()
            mp.SetCurrentFolder(self._bin(mp))
            try:
                comp_item = tl.CreateCompoundClip(list(items.values()), {"name": compound_name})
            finally:
                if prev:
                    mp.SetCurrentFolder(prev)
            if comp_item:
                for idx in range(tl.GetTrackCount("video"), n_before, -1):
                    if not (tl.GetItemListInTrack("video", idx) or []):
                        tl.DeleteTrack("video", idx)
        try:
            tl.SetCurrentTimecode(tc)
        except Exception:
            pass
        return items, comp_item

    def _base_mpi(self, mp, placeholder):
        prev = mp.GetCurrentFolder()
        mp.SetCurrentFolder(self._bin(mp))
        items = mp.ImportMedia([placeholder])
        if prev:
            mp.SetCurrentFolder(prev)
        if not items:
            raise ResolveError("下地クリップを読み込めませんでした")
        return items[0]

    def _safe_track(self, tl, rec, track=0, dur=None):
        """リップル挿入しても何も動かないトラック（再生ヘッド以降にクリップがない）を選ぶ"""
        def ok(idx):
            return all(it.GetEnd() <= rec for it in (tl.GetItemListInTrack("video", idx) or []))
        if track:
            if track > tl.GetTrackCount("video") or not ok(track):
                raise ResolveError(f"V{track} には再生ヘッド以降にクリップがあるので置けません"
                                   "（Text+タイトルの挿入でずれてしまうため）。トラックを「自動」にしてください")
            return track
        n = tl.GetTrackCount("video")
        # 映像の直上から上へ、再生ヘッド以降が空いている既存トラックを探す（なければトラック追加）
        if not dur:
            dur = int(float(tl.GetSetting("timelineFrameRate") or 30))
        for i in range(self._video_top(tl, rec, rec + dur) + 1, n + 1):
            if ok(i):
                return i
        tl.AddTrack("video")
        return tl.GetTrackCount("video")

    def insert_title(self, placeholder, rec=None, dur=None, seconds=5.0, track=0):
        """本物の Fusion タイトル(Text+)を、指定位置・尺・トラックに安全に置く
        手順: 空きトラックに下地を置いて挿入先にする → 他の全トラックをロック →
              In/Out を打って挿入（リップルはそのトラックだけ・後ろに何もないので何も動かない）→ 下地を消す → 元に戻す"""
        proj, tl, mp = self._ctx()
        if tl is None:
            raise ResolveError("タイムラインが開かれていません")
        fps = float(tl.GetSetting("timelineFrameRate"))
        tc = tl.GetCurrentTimecode()
        if rec is None:
            rec = tc_to_frame(tc, fps)
        if dur is None:
            dur = max(1, int(round(seconds * fps)))
        T = self._safe_track(tl, rec, track, dur)
        base = self._base_mpi(mp, placeholder)
        base.SetMarkInOut(0, dur - 1)
        marks = tl.GetMarkInOut() or {}
        locks = {}
        for kind in ("video", "audio", "subtitle"):
            try:
                for i in range(1, tl.GetTrackCount(kind) + 1):
                    locks[(kind, i)] = tl.GetIsTrackLocked(kind, i)
            except Exception:
                pass
        title = None
        try:
            got = mp.AppendToTimeline([{"mediaPoolItem": base, "startFrame": 0, "endFrame": dur,
                                        "trackIndex": T, "recordFrame": rec, "mediaType": 1}])
            if not got:
                raise ResolveError("下地クリップを置けませんでした")
            for (kind, i) in locks:
                if not (kind == "video" and i == T):
                    tl.SetTrackLock(kind, i, True)
            st = tl.GetStartFrame()
            tl.SetMarkInOut(rec - st, rec - st + dur - 1)
            title = tl.InsertFusionTitleIntoTimeline("Text+")
            tl.ClearMarkInOut()
            # 押し出された下地を消す（このトラックの、自分の下地だけ）
            for it in tl.GetItemListInTrack("video", T) or []:
                mpi = it.GetMediaPoolItem()
                if (it.GetStart() == rec + dur and mpi
                        and mpi.GetClipProperty("File Path") == placeholder):
                    tl.DeleteClips([it])
        finally:
            for (kind, i), v in locks.items():
                try:
                    tl.SetTrackLock(kind, i, v)
                except Exception:
                    pass
            try:
                v = marks.get("video") or marks.get("all")
                if v and "in" in v and "out" in v:
                    tl.SetMarkInOut(v["in"], v["out"])
            except Exception:
                pass
            try:
                tl.SetCurrentTimecode(tc)
            except Exception:
                pass
        if not title or title.GetStart() != rec or title.GetTrackTypeAndIndex()[1] != T:
            raise ResolveError("Text+タイトルを想定どおりに置けませんでした（Edit ページで再生ヘッドの位置を確認してください）")
        return title

    def place_titles(self, placeholder, groups, seconds, track, builder, compound_name=None, rec=None, dur=None):
        """groups ごとに本物の Text+ タイトルを置いて中身を組む -> ({uid: item}, compound_item)"""
        proj, tl, mp = self._ctx()
        tc = tl.GetCurrentTimecode()
        n_before = tl.GetTrackCount("video")
        items = {}
        for i, uid in enumerate(groups):
            tl.SetCurrentTimecode(tc)
            title = self.insert_title(placeholder, rec, dur, seconds, track if i == 0 else 0)
            comp = title.GetFusionCompByIndex(1)
            builder(comp, uid)
            self._refresh(tl, comp)
            items[uid] = title
        comp_item = None
        if compound_name and items:
            prev = mp.GetCurrentFolder()
            mp.SetCurrentFolder(self._bin(mp))
            try:
                comp_item = tl.CreateCompoundClip(list(items.values()), {"name": compound_name})
            finally:
                if prev:
                    mp.SetCurrentFolder(prev)
            if comp_item:
                for idx in range(tl.GetTrackCount("video"), n_before, -1):
                    if not (tl.GetItemListInTrack("video", idx) or []):
                        tl.DeleteTrack("video", idx)
        try:
            tl.SetCurrentTimecode(tc)
        except Exception:
            pass
        return items, comp_item

    def find_textplus_items(self, gid):
        """現在のタイムラインから、指定グループの Text+ 出力クリップを探す -> [(item, [uid...])]"""
        _, tl, _ = self._ctx()
        out = []
        for idx in range(1, tl.GetTrackCount("video") + 1):
            for it in tl.GetItemListInTrack("video", idx) or []:
                tags = [u for g, u in item_tags(it) if g == gid]
                if tags:
                    out.append((it, tags))
        return out

    def rebuild_textplus(self, item, builder):
        _, tl, _ = self._ctx()
        n = item.GetFusionCompCount()
        comp = item.GetFusionCompByIndex(n)
        builder(comp)
        self._refresh(tl, comp)

    def find_mpi(self, png_path):
        """PNGパスからメディアプールのクリップを探す"""
        _, _, mp = self._ctx()

        def walk(folder):
            for c in folder.GetClipList() or []:
                if c.GetClipProperty("File Path") == png_path:
                    return c
            for sub in folder.GetSubFolderList() or []:
                r = walk(sub)
                if r:
                    return r
            return None
        root = mp.GetRootFolder()
        for f in root.GetSubFolderList() or []:
            if f.GetName() == BIN_NAME or f.GetName() in OLD_BIN_NAMES:
                r = walk(f)
                if r:
                    return r
        return walk(root)

    def replace(self, mpi, new_png):
        if not mpi.ReplaceClip(new_png):
            raise ResolveError("クリップの差し替えに失敗しました")
        return True
