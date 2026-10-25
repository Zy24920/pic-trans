#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_baidu_pic_translate.py — baidu_pic_translate.py 纯函数与本地逻辑单元测试
=============================================================================
只测本地逻辑（解析/编码/校验/合成/落盘/重试），不发真实网络请求。

运行：
    python -m unittest discover -v        # 在本项目目录下
或
    python -m unittest test_baidu_pic_translate -v
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import baidu_pic_translate as bpt


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def make_png(size=(10, 10), color=(255, 0, 0)):
    """用 Pillow 生成一张真实 PNG 的字节，供合成/落盘测试使用。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class FakeResponse:
    """模拟 requests.Response，只提供 _post 用到的属性。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ---------------------------------------------------------------------------
# Base64 编解码
# ---------------------------------------------------------------------------
class TestB64(unittest.TestCase):

    def test_encode(self):
        self.assertEqual(bpt._b64encode(b"hello"), "aGVsbG8=")

    def test_decode_normal(self):
        self.assertEqual(bpt._b64decode("aGVsbG8="), b"hello")

    def test_decode_none_and_empty(self):
        self.assertIsNone(bpt._b64decode(None))
        self.assertIsNone(bpt._b64decode(""))
        self.assertIsNone(bpt._b64decode("   "))

    def test_decode_data_uri(self):
        self.assertEqual(
            bpt._b64decode("data:image/png;base64,aGVsbG8="), b"hello")

    def test_decode_strips_whitespace(self):
        self.assertEqual(bpt._b64decode("aGVs\nbG8= "), b"hello")

    def test_decode_fixes_padding(self):
        self.assertEqual(bpt._b64decode("aGVsbG8"), b"hello")

    def test_decode_invalid_returns_none(self):
        # 非法 padding 才会抛错；纯垃圾字符会被默认模式丢弃后得到 b""
        self.assertIsNone(bpt._b64decode("A"))          # padding 非法
        self.assertEqual(bpt._b64decode("!!!!"), b"")   # 全部字符被丢弃


# ---------------------------------------------------------------------------
# 图片格式判断
# ---------------------------------------------------------------------------
class TestImageExt(unittest.TestCase):

    def test_png(self):
        self.assertEqual(bpt._image_ext(b"\x89PNG\r\n\x1a\nrest"), ".png")

    def test_jpeg(self):
        self.assertEqual(bpt._image_ext(b"\xff\xd8\xff\xe0junk"), ".jpg")

    def test_gif(self):
        self.assertEqual(bpt._image_ext(b"GIF89ajunk"), ".gif")

    def test_webp(self):
        data = b"RIFF" + b"\x00" * 4 + b"WEBPVP8 "
        self.assertEqual(bpt._image_ext(data), ".webp")

    def test_bmp(self):
        self.assertEqual(bpt._image_ext(b"BMjunk"), ".bmp")

    def test_fallback_png(self):
        self.assertEqual(bpt._image_ext(b"junk"), ".png")


# ---------------------------------------------------------------------------
# rect / points 解析
# ---------------------------------------------------------------------------
class TestParseRect(unittest.TestCase):

    def test_official_string(self):
        self.assertEqual(bpt._parse_rect("236,71,75,28"), (236, 71, 75, 28))

    def test_string_with_spaces(self):
        self.assertEqual(bpt._parse_rect(" 236 , 71 , 75 , 28 "),
                         (236, 71, 75, 28))

    def test_float_string(self):
        self.assertEqual(bpt._parse_rect("1.5,2,3,4"), (1, 2, 3, 4))

    def test_list_and_tuple(self):
        self.assertEqual(bpt._parse_rect([1, 2, 3, 4]), (1, 2, 3, 4))
        self.assertEqual(bpt._parse_rect((1.0, 2.0, 3.0, 4.0)), (1, 2, 3, 4))

    def test_extra_values_take_first_four(self):
        self.assertEqual(bpt._parse_rect("1,2,3,4,5"), (1, 2, 3, 4))

    def test_invalid(self):
        self.assertIsNone(bpt._parse_rect(None))
        self.assertIsNone(bpt._parse_rect("abc"))
        self.assertIsNone(bpt._parse_rect("1,2,3"))
        self.assertIsNone(bpt._parse_rect(12345))


class TestParsePoints(unittest.TestCase):

    def test_dict_points(self):
        self.assertEqual(bpt._parse_points([{"x": 1, "y": 2}]),
                         [{"x": 1.0, "y": 2.0}])

    def test_list_points(self):
        self.assertEqual(bpt._parse_points([[3, 4]]), [{"x": 3.0, "y": 4.0}])

    def test_invalid_entries_skipped(self):
        points = [{"x": "a", "y": 2}, {"x": 1}, [1], "junk", {"x": 1.5, "y": 2.5}]
        self.assertEqual(bpt._parse_points(points), [{"x": 1.5, "y": 2.5}])

    def test_non_list_returns_empty(self):
        self.assertEqual(bpt._parse_points("notalist"), [])
        self.assertEqual(bpt._parse_points(None), [])


# ---------------------------------------------------------------------------
# error_code / raw 清洗
# ---------------------------------------------------------------------------
class TestErrorCode(unittest.TestCase):

    def test_normal(self):
        self.assertEqual(bpt._error_code({"error_code": "53001"}), "53001")
        self.assertEqual(bpt._error_code({"error_code": 53001}), "53001")
        self.assertEqual(bpt._error_code({"error_code": " 54001 "}), "54001")

    def test_success_codes_treated_as_none(self):
        self.assertIsNone(bpt._error_code({"error_code": "0"}))
        self.assertIsNone(bpt._error_code({"error_code": "200"}))
        self.assertIsNone(bpt._error_code({"error_code": None}))

    def test_missing_or_non_dict(self):
        self.assertIsNone(bpt._error_code({}))
        self.assertIsNone(bpt._error_code("string"))
        self.assertIsNone(bpt._error_code(None))


class TestSanitizeRaw(unittest.TestCase):

    def test_top_level(self):
        cleaned = bpt._sanitize_raw({"paste_img": "BASE64", "a": 1})
        self.assertEqual(cleaned, {"paste_img": "<base64 omitted>", "a": 1})

    def test_content_key(self):
        self.assertEqual(bpt._sanitize_raw({"content": "xxx"}),
                         {"content": "<base64 omitted>"})

    def test_nested(self):
        cleaned = bpt._sanitize_raw(
            {"a": {"paste_img": "b"}, "list": [{"content": "c"}]})
        self.assertEqual(
            cleaned,
            {"a": {"paste_img": "<base64 omitted>"},
             "list": [{"content": "<base64 omitted>"}]})

    def test_plain_value_passthrough(self):
        self.assertEqual(bpt._sanitize_raw("plain"), "plain")


# ---------------------------------------------------------------------------
# 参数校验 / 异常
# ---------------------------------------------------------------------------
class TestValidateOptions(unittest.TestCase):

    def test_valid_passes(self):
        bpt._validate_options("auto", "zh", 1, 1, 0, "nmt")

    def test_unknown_langs_only_warn(self):
        bpt._validate_options("xx", "yy", 1, 1, 0, "nmt")   # 不应抛异常

    def test_target_auto_rejected(self):
        with self.assertRaises(bpt.InputError):
            bpt._validate_options("auto", "auto", 1, 1, 0, "nmt")

    def test_bad_paste(self):
        with self.assertRaises(bpt.InputError):
            bpt._validate_options("auto", "zh", 5, 1, 0, "nmt")

    def test_bad_view_type(self):
        with self.assertRaises(bpt.InputError):
            bpt._validate_options("auto", "zh", 1, 2, 0, "nmt")

    def test_bad_need_intervene(self):
        with self.assertRaises(bpt.InputError):
            bpt._validate_options("auto", "zh", 1, 1, 2, "nmt")

    def test_bad_model_type(self):
        with self.assertRaises(bpt.InputError):
            bpt._validate_options("auto", "zh", 1, 1, 0, "other")


class TestApiError(unittest.TestCase):

    def test_known_code_message(self):
        err = bpt.APIError("53001")
        self.assertEqual(err.code, "53001")
        self.assertIn("服务器内部错误", str(err))
        self.assertTrue(str(err).startswith("[53001]"))

    def test_custom_message_and_raw(self):
        err = bpt.APIError("99999", "custom", {"k": 1})
        self.assertEqual(str(err), "[99999] custom")
        self.assertEqual(err.raw, {"k": 1})


# ---------------------------------------------------------------------------
# 图片读取 / 输入收集
# ---------------------------------------------------------------------------
class TestReadImage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_missing_file(self):
        with self.assertRaises(bpt.InputError):
            bpt._read_image(self.dir / "nope.png")

    def test_empty_file(self):
        p = self.dir / "empty.png"
        p.write_bytes(b"")
        with self.assertRaises(bpt.InputError):
            bpt._read_image(p)

    def test_valid_png(self):
        p = self.dir / "a.png"
        p.write_bytes(make_png())
        self.assertEqual(bpt._read_image(p), make_png())

    def test_unsupported_format_warns_but_returns(self):
        p = self.dir / "a.gif"
        p.write_bytes(b"GIF89ajunk")
        self.assertEqual(bpt._read_image(p), b"GIF89ajunk")


class TestCollectInputs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_directory_filters_and_recurses(self):
        (self.dir / "a.png").write_bytes(b"x")
        (self.dir / "b.jpg").write_bytes(b"x")
        (self.dir / "c.gif").write_bytes(b"x")          # 目录递归时应被排除
        sub = self.dir / "sub"
        sub.mkdir()
        (sub / "d.png").write_bytes(b"x")
        found = bpt._collect_inputs([str(self.dir)])
        names = [f.name for f in found]
        self.assertEqual(names, ["a.png", "b.jpg", "d.png"])

    def test_direct_file_no_ext_filter(self):
        p = self.dir / "e.txt"
        p.write_bytes(b"x")
        found = bpt._collect_inputs([str(p)])
        self.assertEqual([f.name for f in found], ["e.txt"])

    def test_missing_path_skipped(self):
        self.assertEqual(bpt._collect_inputs([str(self.dir / "nope")]), [])

    def test_dedup_keeps_order(self):
        p = self.dir / "a.png"
        p.write_bytes(b"x")
        found = bpt._collect_inputs([str(p), str(p)])
        self.assertEqual(len(found), 1)


class TestUniqueStem(unittest.TestCase):

    def test_collision_appends_index(self):
        used = set()
        self.assertEqual(bpt._unique_stem("img", used), "img")
        self.assertEqual(bpt._unique_stem("img", used), "img_2")
        self.assertEqual(bpt._unique_stem("img", used), "img_3")


# ---------------------------------------------------------------------------
# 区块合成（compose_blocks）
# ---------------------------------------------------------------------------
class TestComposeBlocks(unittest.TestCase):

    def setUp(self):
        self.source = make_png((100, 50), (0, 0, 0))

    def _block(self, rect, size=(20, 10), color=(255, 0, 0)):
        return bpt.Block(index=1, rect=rect, paste_img=make_png(size, color))

    def test_basic_compose(self):
        out = bpt.compose_blocks(self.source, [self._block((10, 10, 20, 10))])
        from PIL import Image
        img = Image.open(io.BytesIO(out)).convert("RGB")
        self.assertEqual(img.size, (100, 50))
        self.assertEqual(img.getpixel((15, 15)), (255, 0, 0))   # 贴合区内
        self.assertEqual(img.getpixel((5, 5)), (0, 0, 0))       # 贴合区外

    def test_out_of_bounds_clipped(self):
        # rect 超出右下边界，应被裁剪而不是抛异常
        out = bpt.compose_blocks(self.source, [self._block((90, 45, 20, 10))])
        from PIL import Image
        img = Image.open(io.BytesIO(out)).convert("RGB")
        self.assertEqual(img.size, (100, 50))
        self.assertEqual(img.getpixel((92, 47)), (255, 0, 0))

    def test_blocks_without_img_or_rect_skipped(self):
        empty = bpt.Block(index=1)
        out = bpt.compose_blocks(self.source, [empty])
        from PIL import Image
        img = Image.open(io.BytesIO(out)).convert("RGB")
        self.assertEqual(img.getpixel((50, 25)), (0, 0, 0))


# ---------------------------------------------------------------------------
# TranslateResult.save 落盘
# ---------------------------------------------------------------------------
class TestTranslateResultSave(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def _make_result(self):
        return bpt.TranslateResult(
            source_lang="auto",
            target_lang="en",
            src="hello",
            dst="hi",
            full_image=make_png((10, 10), (0, 255, 0)),
            blocks=[bpt.Block(index=1, rect=(0, 0, 10, 10),
                              paste_img=make_png((10, 10), (0, 0, 255)))],
            request={"paste": 2},
            raw={"paste_img": "BIGB64"},
            source_image=make_png((10, 10), (0, 0, 0)),
            elapsed=0.5,
        )

    def test_save_all_artifacts(self):
        result = self._make_result()
        paths = result.save(self.out, "test", compose=True)
        self.assertTrue(Path(paths["text"]).is_file())
        self.assertTrue(Path(paths["image"]).is_file())
        self.assertTrue(Path(paths["composed"]).is_file())
        self.assertEqual(len(paths["blocks"]), 1)
        self.assertTrue(Path(paths["json"]).is_file())

        text = Path(paths["text"]).read_text(encoding="utf-8-sig")
        self.assertIn("hello", text)
        self.assertIn("hi", text)

        data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
        self.assertEqual(data["dst"], "hi")
        self.assertEqual(data["raw"]["paste_img"], "<base64 omitted>")

    def test_save_no_text(self):
        paths = self._make_result().save(
            self.out, "test", compose=False, save_text=False)
        self.assertIsNone(paths["text"])


# ---------------------------------------------------------------------------
# PictureTranslator 本地校验（不发网络请求）
# ---------------------------------------------------------------------------
class TestTranslatorLocalChecks(unittest.TestCase):

    def test_missing_credentials(self):
        translator = bpt.PictureTranslator(appid="", token="")
        with self.assertRaises(bpt.ConfigError):
            translator.translate_bytes(b"x")

    def test_empty_data(self):
        translator = bpt.PictureTranslator(appid="a", token="t")
        with self.assertRaises(bpt.InputError):
            translator.translate_bytes(b"")

    def test_oversized_data(self):
        translator = bpt.PictureTranslator(appid="a", token="t")
        big = b"\x00" * (4 * 1024 * 1024)      # Base64 后约 5.6MB > 5MB
        with self.assertRaises(bpt.InputError):
            translator.translate_bytes(big)


# ---------------------------------------------------------------------------
# _post 网络层：重试与错误分类（mock，不联网）
# ---------------------------------------------------------------------------
class TestPostRetries(unittest.TestCase):

    def setUp(self):
        self.translator = bpt.PictureTranslator(appid="a", token="t", retries=1)
        # 跳过退避 sleep，加快测试
        patcher = mock.patch.object(bpt, "_sleep_backoff", lambda a: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_first_try(self):
        with mock.patch.object(
                requests, "post",
                return_value=FakeResponse(200, {"ok": 1})) as mp:
            data = self.translator._post({"a": 1})
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(mp.call_count, 1)

    def test_connection_error_then_success(self):
        with mock.patch.object(
                requests, "post",
                side_effect=[requests.ConnectionError("x"),
                             FakeResponse(200, {"ok": 1})]) as mp:
            data = self.translator._post({"a": 1})
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(mp.call_count, 2)

    def test_500_retries_then_network_error(self):
        with mock.patch.object(
                requests, "post",
                side_effect=lambda *a, **k: FakeResponse(500, {}, "boom")) as mp:
            with self.assertRaises(bpt.NetworkError):
                self.translator._post({"a": 1})
        self.assertEqual(mp.call_count, 2)      # 1 次原始 + 1 次重试

    def test_non_retryable_api_error_immediate(self):
        with mock.patch.object(
                requests, "post",
                side_effect=[FakeResponse(200, {"error_code": "54001",
                                                "error_msg": "token"})]) as mp:
            with self.assertRaises(bpt.APIError) as ctx:
                self.translator._post({"a": 1})
        self.assertEqual(ctx.exception.code, "54001")
        self.assertEqual(mp.call_count, 1)      # 余额/token 类错误不重试

    def test_retryable_api_error_retries(self):
        with mock.patch.object(
                requests, "post",
                side_effect=[FakeResponse(200, {"error_code": "59004"}),
                             FakeResponse(200, {"error_code": "59004"})]) as mp:
            with self.assertRaises(bpt.APIError):
                self.translator._post({"a": 1})
        self.assertEqual(mp.call_count, 2)

    def test_non_json_response(self):
        with mock.patch.object(
                requests, "post",
                side_effect=[FakeResponse(200, None, "<html>")]):
            with self.assertRaises(bpt.NetworkError):
                self.translator._post({"a": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
