"""Unit tests for `jy render --remote`: Mac -> Windows path rewrite, mp4 check, errors.

No PC, no network: the PC's answers are a hand-written probe dict.
Run: python3 -m unittest discover -s tests -p 'test_remote.py'
"""

import base64
import json
import os
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jy_cli import remote  # noqa: E402
from jy_cli.envelope import JyError  # noqa: E402

CACHE = "/Users/u/Movies/JianyingPro/User Data/Cache"
DRAFT = "/Users/u/work/我的草稿"
FONT = "/Applications/VideoFusion-macOS.app/Contents/Resources/Font/SystemFont/zh-hans.ttf"
FONT2 = "/Applications/VideoFusion-macOS.app/Contents/Resources/Font/Other/odd.ttf"
FONT3 = "/Applications/VideoFusion-macOS.app/Contents/Resources/Font/Gone/gone.ttf"
EFFECT_HAVE = f"{CACHE}/effect/111111111/aaaa"
EFFECT_OTHER = f"{CACHE}/effect/222222222/bbbb"
EFFECT_MISSING = f"{CACHE}/artistEffect/333333333/cccc"
IMG_CACHE = f"{CACHE}/image/x.png"
MEDIA = "/Users/u/Movies/clip one.mp4"
USER_FONT = "/Users/u/Library/Fonts/SmileySans-Oblique.ttf"
GONE = "/Users/u/gone.wav"

JOB = "C:/Users/J/studio/jobs/jianying/jy-1"
PROBE = {
    "job_dir": JOB,
    "app_resources": "C:/Users/J/AppData/Local/JianyingPro/Apps/11.8.0.14778/Resources",
    "cache_root": "C:/Users/J/AppData/Local/JianyingPro/User Data/Cache",
    "app_exists": {"Font/SystemFont/zh-hans.ttf": True, "Font/Other/odd.ttf": False,
                   "Font/Gone/gone.ttf": False},
    "font_alt": {"Font/Other/odd.ttf": "C:/X/Font/Elsewhere/odd.ttf"},
    "cache_exists": {"image/x.png": False},
    "packages": {"effect/111111111": ["aaaa"], "effect/222222222": ["zzzz"],
                 "artistEffect/333333333": []},
}
LOCAL = {MEDIA, USER_FONT, EFFECT_MISSING, EFFECT_MISSING + "_modity_time.txt", IMG_CACHE, FONT3}


def draft_doc():
    return {
        "materials": {
            "videos": [{"path": MEDIA}],
            "texts": [{"font_path": FONT,
                       "content": json.dumps({"styles": [{"font": {"path": FONT}}]})},
                      {"font_path": USER_FONT}, {"font_path": FONT2}, {"font_path": FONT3}],
            "video_effects": [{"path": EFFECT_HAVE}, {"path": EFFECT_OTHER + "/sub/a.json"}],
            "effects": [{"path": EFFECT_MISSING}],
            "images": [{"path": IMG_CACHE}],
            "audios": [{"path": GONE}],
            "local": [{"path": DRAFT + "/Resources/a.png"}],
        },
        "meta": {"draft_fold_path": DRAFT, "draft_root_path": "/Users/u/work"},
        "not_paths": ["", "/", "//cdn.example/x", "hello"],
    }


def make_plan():
    doc = draft_doc()
    paths = remote.collect_paths(doc)
    return doc, remote.plan(paths, DRAFT, PROBE, cache_root=CACHE,
                            exists=lambda p: p in LOCAL, isdir=lambda p: False)


class CollectTest(unittest.TestCase):
    def test_collects_only_absolute_paths(self):
        paths = remote.collect_paths(draft_doc())
        self.assertIn(MEDIA, paths)
        self.assertIn(DRAFT, paths)
        self.assertNotIn("/", paths)
        self.assertNotIn("//cdn.example/x", paths)
        self.assertNotIn("hello", paths)

    def test_classify(self):
        c = lambda p: remote.classify(p, DRAFT, cache_root=CACHE)  # noqa: E731
        self.assertEqual(c(DRAFT), ("draft", ""))
        self.assertEqual(c(DRAFT + "/a/b.png"), ("draft", "a/b.png"))
        self.assertEqual(c("/Users/u/work"), ("draft_parent", "我的草稿"))
        self.assertEqual(c(FONT), ("app", "Font/SystemFont/zh-hans.ttf"))
        self.assertEqual(c(EFFECT_HAVE), ("cache", "effect/111111111/aaaa"))
        self.assertEqual(c(MEDIA), ("media", None))
        # a sibling whose name only starts like the draft is not inside it
        self.assertEqual(c(DRAFT + "2/x.mp4"), ("media", None))

    def test_classify_accepts_draft_aliases(self):
        kind, _ = remote.classify("/tmp/d/x.png", ("/private/tmp/d", "/tmp/d"), cache_root=CACHE)
        self.assertEqual(kind, "draft")

    def test_probe_request(self):
        req = remote.probe_request(remote.collect_paths(draft_doc()), DRAFT, cache_root=CACHE)
        self.assertIn("Font/SystemFont/zh-hans.ttf", req["app_rels"])
        self.assertEqual(req["package_keys"],
                         ["artistEffect/333333333", "effect/111111111", "effect/222222222"])
        self.assertEqual(req["cache_rels"], ["image/x.png"])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.doc, self.plan = make_plan()
        self.m = self.plan["mapping"]

    def test_app_font_to_pc_app_dir(self):
        self.assertEqual(self.m[FONT], PROBE["app_resources"] + "/Font/SystemFont/zh-hans.ttf")

    def test_app_font_found_by_name(self):
        self.assertEqual(self.m[FONT2], "C:/X/Font/Elsewhere/odd.ttf")

    def test_app_file_missing_on_pc_is_copied(self):
        self.assertEqual(self.m[FONT3], f"{JOB}/res/Font/Gone/gone.ttf")

    def test_effect_already_on_pc(self):
        self.assertEqual(self.m[EFFECT_HAVE], PROBE["cache_root"] + "/effect/111111111/aaaa")

    def test_effect_other_version_on_pc(self):
        self.assertEqual(self.m[EFFECT_OTHER + "/sub/a.json"],
                         PROBE["cache_root"] + "/effect/222222222/zzzz/sub/a.json")

    def test_missing_effect_is_installed_into_pc_cache(self):
        self.assertEqual(self.m[EFFECT_MISSING], PROBE["cache_root"] + "/artistEffect/333333333/cccc")
        self.assertEqual(self.plan["installs"], ["artistEffect/333333333/cccc"])
        copies = dict(self.plan["copies"])
        self.assertEqual(copies[EFFECT_MISSING], "install/artistEffect/333333333/cccc")
        self.assertEqual(copies[EFFECT_MISSING + "_modity_time.txt"],
                         "install/artistEffect/333333333/cccc_modity_time.txt")

    def test_other_cache_file_is_copied(self):
        self.assertEqual(self.m[IMG_CACHE], f"{JOB}/cache/image/x.png")

    def test_media_and_user_fonts_get_ascii_names(self):
        self.assertRegex(self.m[MEDIA], rf"^{JOB}/media/\d{{3}}_clip_one\.mp4$")
        self.assertRegex(self.m[USER_FONT], rf"^{JOB}/media/\d{{3}}_SmileySans-Oblique\.ttf$")
        for _src, rel in self.plan["copies"]:
            rel.encode("ascii")

    def test_draft_paths(self):
        self.assertEqual(self.m[DRAFT], f"{JOB}/draft")
        self.assertEqual(self.m[DRAFT + "/Resources/a.png"], f"{JOB}/draft/Resources/a.png")
        self.assertEqual(self.m["/Users/u/work"], JOB)

    def test_missing_local_file_is_unresolved_not_invented(self):
        self.assertNotIn(GONE, self.m)
        self.assertEqual(self.plan["unresolved"], [{"path": GONE, "kind": "media"}])

    def test_rewrite_leaves_no_mac_paths(self):
        fixed = remote.rewrite(self.doc, self.m)
        text = json.dumps(fixed, ensure_ascii=False)
        for mac in (FONT, MEDIA, USER_FONT, EFFECT_HAVE, EFFECT_MISSING, DRAFT, IMG_CACHE):
            self.assertNotIn(mac, text)
        # the font path embedded in the JSON-in-a-string content is rewritten too
        inner = json.loads(fixed["materials"]["texts"][0]["content"])
        self.assertEqual(inner["styles"][0]["font"]["path"], self.m[FONT])
        # unresolved paths stay as they were
        self.assertEqual(fixed["materials"]["audios"][0]["path"], GONE)
        self.assertEqual(fixed["not_paths"], draft_doc()["not_paths"])

    def test_rewrite_respects_path_boundaries(self):
        doc = {"a": f"see {DRAFT}2/x.mp4 and {DRAFT}/y.png"}
        fixed = remote.rewrite(doc, {DRAFT: f"{JOB}/draft"})
        self.assertEqual(fixed["a"], f"see {DRAFT}2/x.mp4 and {JOB}/draft/y.png")


class NamesTest(unittest.TestCase):
    def test_windows_name(self):
        self.assertEqual(remote.windows_name('a:b/c*?"<>|.'), "a_b_c______")
        self.assertEqual(remote.windows_name("远程 草稿"), "远程 草稿")
        self.assertEqual(remote.windows_name("..."), "draft")


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind.encode()) + payload


def fake_moov(seconds=3, width=1280, height=720):
    mvhd = _box("mvhd", b"\0" * 12 + struct.pack(">II", 1000, seconds * 1000) + b"\0" * 80)
    tkhd = _box("tkhd", b"\0" * 76 + struct.pack(">II", width << 16, height << 16))
    hdlr = _box("hdlr", b"\0" * 8 + b"vide" + b"\0" * 12)
    stsd = _box("stsd", b"\0" * 8 + struct.pack(">I", 100) + b"avc1" + b"\0" * 8)
    mdia = _box("mdia", hdlr + _box("minf", _box("stbl", stsd)))
    a_hdlr = _box("hdlr", b"\0" * 8 + b"soun" + b"\0" * 12)
    a_stsd = _box("stsd", b"\0" * 8 + struct.pack(">I", 100) + b"mp4a" + b"\0" * 8)
    a_trak = _box("trak", _box("tkhd", b"\0" * 84) + _box("mdia", a_hdlr + _box("minf", _box("stbl", a_stsd))))
    return _box("moov", mvhd + _box("trak", tkhd + mdia) + a_trak)


class Mp4Test(unittest.TestCase):
    def test_parse_moov(self):
        info = remote.parse_moov(fake_moov())
        self.assertEqual(info["duration_s"], 3.0)
        video = [t for t in info["tracks"] if t.get("type") == "vide"][0]
        self.assertEqual((video["width"], video["height"], video["codec"]), (1280, 720, "avc1"))

    def test_remote_output_info(self):
        out = remote.remote_output_info({
            "output_path": "C:/o/a.mp4", "bytes": 1000, "boxes": ["ftyp", "moov", "mdat"],
            "moov_b64": base64.b64encode(fake_moov()).decode()})
        self.assertEqual((out["codec"], out["width"], out["audio_codec"]), ("h264", 1280, "aac"))

    def test_incomplete_mp4_is_an_error(self):
        with self.assertRaises(JyError):
            remote.remote_output_info({"boxes": ["ftyp", "mdat"], "moov_b64": None})


class ErrorTest(unittest.TestCase):
    def setUp(self):
        self.r = remote.Remote("gamingpc", job_id="jy-test")

    def code(self, probe, launch=False):
        err = remote._probe_errors(self.r, probe, launch=launch)
        return err and (err.code, err.message)

    def test_game_mode_blocks(self):
        code, msg = self.code({"ok": False, "error": "game_mode", "game_mode": "on"})
        self.assertEqual(code, "BLOCKED")
        self.assertIn("游戏", msg)

    def test_not_running_points_at_relaunch(self):
        code, msg = self.code({"ok": True, "bridge": {"state": "not_running"}})
        self.assertEqual(code, "UNAVAILABLE")
        self.assertIn("--relaunch-app", msg)

    def test_wedged(self):
        self.assertIn("卡死", self.code({"ok": True, "bridge": {"state": "wedged"}})[1])

    def test_not_logged_in(self):
        code, msg = self.code({"ok": True, "bridge": {"state": "ok"}, "logged_in": False})
        self.assertIn("登录", msg)

    def test_ready(self):
        self.assertIsNone(remote._probe_errors(self.r, {"ok": True, "bridge": {"state": "ok"},
                                                        "logged_in": True}, launch=False))

    def test_bad_host_and_job(self):
        with self.assertRaises(JyError):
            remote.Remote("host; rm -rf /")
        with self.assertRaises(JyError):
            remote.Remote("gamingpc", job_id="..\\x")

    def test_unreachable_host_fails_fast(self):
        r = remote.Remote("jy-no-such-host.invalid", job_id="jy-test")
        with self.assertRaises(JyError) as ctx:
            r.mkdir()
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")
        self.assertIn("连不上", ctx.exception.message)


class DraftFilesTest(unittest.TestCase):
    def test_skips_encrypted_backup_and_bookkeeping(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "draft_info.json").write_text('{"a": "/x/y"}', "utf-8")
            Path(tmp, "draft_info.json.bak").write_text('{"a": "/b"}', "utf-8")
            Path(tmp, ".jy-spec.json").write_text('{"a": "/c"}', "utf-8")
            Path(tmp, "enc.json").write_bytes(b"HZ0abc==")
            names = [p.name for p, _ in remote.draft_json_files(tmp)]
            self.assertEqual(names, ["draft_info.json"])


class PcMediaTest(unittest.TestCase):
    """Media already on the PC: Windows paths pass through untouched; --remote-output."""

    def test_windows_paths_are_not_collected_or_rewritten(self):
        doc = {"materials": {"videos": [{"path": "C:/Users/J/studio/projects/p/00_source/media/source.mov"},
                                        {"path": MEDIA}]}}
        self.assertEqual(remote.collect_paths(doc), {MEDIA})
        fixed = remote.rewrite(doc, {MEDIA: "C:/job/media/001.mp4"})
        self.assertEqual(fixed["materials"]["videos"][0]["path"],
                         "C:/Users/J/studio/projects/p/00_source/media/source.mov")

    def test_remote_output_rel(self):
        rel = "studio/projects/2026-11-21-口播/03_content-lock/v1/video/content-only.mp4"
        self.assertEqual(remote.remote_output_rel(rel), rel)
        self.assertEqual(remote.remote_output_rel("studio\\x\\a.mp4"), "studio/x/a.mp4")
        self.assertIsNone(remote.remote_output_rel(None))
        for bad in ("../x.mp4", "C:/Users/J/x.mp4", "studio/a:b/x.mp4", "studio/x.mov", "studio//x.mp4"):
            with self.assertRaises(JyError, msg=bad):
                remote.remote_output_rel(bad)

    def test_is_windows_path(self):
        from jy_cli import ffmpeg

        self.assertTrue(ffmpeg.is_windows_path("C:/a/b.mov"))
        self.assertTrue(ffmpeg.is_windows_path("d:\\a.mov"))
        self.assertFalse(ffmpeg.is_windows_path("/Users/u/a.mov"))
        self.assertFalse(ffmpeg.is_windows_path("C:a.mov"))

    def test_remote_probe_rejects_bad_input_before_ssh(self):
        from jy_cli import ffmpeg

        with self.assertRaises(JyError):
            ffmpeg.probe_media_remote("bad host", "C:/a.mov")
        with self.assertRaises(JyError):
            ffmpeg.probe_media_remote("gamingpc", "C:/it's.mov")
        with self.assertRaises(JyError):
            ffmpeg.probe_media_remote("gamingpc", "/Users/u/a.mov")

    def test_draft_build_with_pc_media(self):
        import tempfile
        from unittest import mock
        from jy_cli import draft, ffmpeg

        pc = "C:/Users/J/studio/projects/p/00_source/media/source.mov"
        raw = {"format": {"duration": "12.5", "size": "1000"},
               "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 1080, "height": 1920,
                            "r_frame_rate": "30/1"},
                           {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]}
        fake = dict(ffmpeg._normalize(pc, raw, local=False), host="gamingpc")
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp, "spec.json")
            spec.write_text(json.dumps({"clips": [{"path": pc, "source_start": 1, "duration": 5}]}), "utf-8")
            with self.assertRaises(JyError) as ctx:
                draft.build(None, spec, Path(tmp, "d1"))
            self.assertIn("--media-host", ctx.exception.message)
            with mock.patch.object(ffmpeg, "probe_media_remote", return_value=fake) as probe:
                data = draft.build(None, spec, Path(tmp, "d2"), media_host="gamingpc")
            probe.assert_called_once_with("gamingpc", pc)
            info = json.loads(Path(data["draft_info"]).read_text("utf-8"))
            self.assertEqual(info["materials"]["videos"][0]["path"], pc)
            self.assertEqual(info["canvas_config"]["width"], 1080)


if __name__ == "__main__":
    unittest.main()
