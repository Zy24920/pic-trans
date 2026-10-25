#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度图片翻译 V2.0 一体化脚本
=============================================================================
接口：https://fanyi-api.baidu.com/ait/api/picture/translate  （POST / JSON / Bearer Token）
文档：https://fanyi-api.baidu.com/product/233

本脚本把官方 demo 封装成三种可用形态：
  1. 命令行：直接翻译单张/多张图片或整个目录
     python baidu_pic_translate.py -i test.png -f zh -t en -p 1 -o output
     python baidu_pic_translate.py -i imgs/ -t en -p 2 --compose -o output --workers 4
  2. 模块：被其它 Python 代码 import 后调用
     from baidu_pic_translate import PictureTranslator, translate_image
     res = translate_image("test.png", target_lang="en", paste=1)
     res.save("output", "test")
  3. 配置：--appid/--token 或环境变量 BAIDU_FANYI_APPID / BAIDU_FANYI_TOKEN

覆盖全部官方能力（paste 四种贴合方案 / 两档擦除精度 / 术语干预 / 语种自动识别）：
  paste=0  只返回 OCR 原文与译文（不返回图片）
  paste=1  整图贴合：返回翻译后整图 base64
  paste=2  区块贴合：返回每个文字块的贴合小图 + 坐标
  paste=3  透明底图：只返回译文坐标点，不返回图片（由调用方自行渲染）
  view_type=0 通用擦除 / view_type=1 高精擦除
  need_intervene=1 启用后台配置的术语库干预

注意：
  - 图片以 Base64 传给接口，Base64 后大小上限 5MB（错误码 55005）。
  - 术语干预使用控制台里配置的术语库，接口本身没有 term 参数。
  - 返回值里的 base64 图片可能是 PNG 或 JPEG，脚本按文件头自动判断后缀。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

# =============================================================================
# 常量 / 官方枚举
# =============================================================================
API_URL = "https://fanyi-api.baidu.com/ait/api/picture/translate"

ENV_APPID = "BAIDU_FANYI_APPID"
ENV_TOKEN = "BAIDU_FANYI_TOKEN"

# Base64 编码后的字符串上限（官方：Base64 后不超过 5MB）
MAX_B64_SIZE = 5 * 1024 * 1024

# 文档列出的语种简写（源语言额外支持 auto）
SOURCE_LANGS = [
    "auto", "zh", "en", "jp", "kor", "fra", "spa", "ru", "pt", "de",
    "it", "dan", "nl", "may", "swe", "id", "pl", "rom", "tr", "el", "hu",
]
TARGET_LANGS = [x for x in SOURCE_LANGS if x != "auto"]

LANG_NAMES = {
    "auto": "自动识别（仅源语言可用）",
    "zh": "中文", "en": "英语", "jp": "日语", "kor": "韩语",
    "fra": "法语", "spa": "西班牙语", "ru": "俄语", "pt": "葡萄牙语",
    "de": "德语", "it": "意大利语", "dan": "丹麦语", "nl": "荷兰语",
    "may": "马来语", "swe": "瑞典语", "id": "印尼语", "pl": "波兰语",
    "rom": "罗马尼亚语", "tr": "土耳其语", "el": "希腊语", "hu": "匈牙利语",
}

PASTE_MODES = {
    0: "关闭贴合：只返回 OCR 原文/译文文本",
    1: "整图贴合：返回翻译后的整张图片（paste_img）",
    2: "区块贴合：返回每个文字块的贴合小图与坐标（contents[].paste_img / rect）",
    3: "透明底图：只返回译文坐标点，不返回图片（适合自行渲染）",
}

VIEW_TYPES = {
    0: "通用擦除",
    1: "高精擦除（效果更好，耗时略长）",
}

# V2.0 图片翻译错误码
ERROR_CODES = {
    "53001": "服务器内部错误，请联系技术支持",
    "54000": "必填参数为空",
    "54001": "签名或者 token 错误",
    "54004": "账户余额不足",
    "55001": "参数校验失败（from/to/paste/model_type 等）",
    "55002": "token 校验失败",
    "55003": "图片解析服务报错（图片模糊、排版复杂或文字无法识别）",
    "55004": "图片翻译服务报错（贴合渲染异常）",
    "55005": "文件大小超过上限（Base64 后超过 5MB）",
    "55006": "图片翻译服务未开通/已停用",
    "55007": "签名校验失败",
    "59004": "请求 QPS 超限，请稍后重试",
}

# 这些错误可以重试，其它错误（余额、参数、token）重试没有意义
RETRYABLE_CODES = {"53001", "55004", "59004"}

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


# =============================================================================
# 异常
# =============================================================================
class PicTransError(Exception):
    """脚本内所有可预期异常的基类。"""


class ConfigError(PicTransError):
    """缺少 APPID / TOKEN 等配置。"""


class InputError(PicTransError):
    """图片读取、格式或大小不合法。"""


class NetworkError(PicTransError):
    """网络层错误（超时、连接失败、5xx）。"""


class APIError(PicTransError):
    """接口返回了 error_code。"""

    def __init__(self, code: str, message: str = "", raw: Optional[dict] = None):
        self.code = str(code)
        self.message = message or ERROR_CODES.get(self.code, "未知错误")
        self.raw = raw or {}
        super().__init__(f"[{self.code}] {self.message}")

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# =============================================================================
# 数据结构
# =============================================================================
@dataclass
class Block:
    """一个文字块的翻译结果。"""

    index: int
    src: str = ""
    dst: str = ""
    rect: Optional[Tuple[int, int, int, int]] = None  # (x, y, w, h)
    line_count: int = 0
    points: List[Dict[str, float]] = field(default_factory=list)
    paste_img: Optional[bytes] = field(default=None, repr=False)

    def to_dict(self, embed_images: bool = False) -> dict:
        return {
            "index": self.index,
            "src": self.src,
            "dst": self.dst,
            "rect": list(self.rect) if self.rect else None,
            "line_count": self.line_count,
            "points": self.points,
            "paste_img": _b64encode(self.paste_img) if (embed_images and self.paste_img) else "",
        }


@dataclass
class TranslateResult:
    """一次图片翻译的完整结果，可直接 save() 落盘。"""

    source_lang: str
    target_lang: str
    src: str = ""
    dst: str = ""
    full_image: Optional[bytes] = field(default=None, repr=False)   # paste=1
    blocks: List[Block] = field(default_factory=list)
    request: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    source_image: Optional[bytes] = field(default=None, repr=False)  # 原图，用于 --compose
    elapsed: float = 0.0

    # ---------------------------------------------------------------- 导出
    def to_dict(self, embed_images: bool = False) -> dict:
        return {
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "src": self.src,
            "dst": self.dst,
            "request": self.request,
            "blocks": [b.to_dict(embed_images=embed_images) for b in self.blocks],
            "raw": _sanitize_raw(self.raw),
        }

    def save(
        self,
        out_dir: str | os.PathLike,
        stem: str,
        *,
        compose: bool = False,
        embed_images: bool = False,
        save_text: bool = True,
    ) -> Dict[str, Optional[str]]:
        """把结果写入磁盘，返回各产物的路径（未生成的为 None）。"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        lang = self.target_lang or "out"
        paths: Dict[str, Optional[str]] = {
            "json": None,
            "text": None,
            "image": None,
            "composed": None,
            "blocks": [],
        }

        if save_text:
            text_path = out / f"{stem}_{lang}_text.txt"
            text_path.write_text(
                f"[{self.source_lang} -> {self.target_lang}]\n"
                f"--- 原文 ---\n{self.src}\n\n--- 译文 ---\n{self.dst}\n",
                encoding="utf-8-sig",
            )
            paths["text"] = str(text_path)

        if self.full_image:
            img_path = out / f"{stem}_{lang}{_image_ext(self.full_image)}"
            img_path.write_bytes(self.full_image)
            paths["image"] = str(img_path)

        block_paths: List[str] = []
        for b in self.blocks:
            if not b.paste_img:
                continue
            bp = out / f"{stem}_{lang}_block_{b.index:02d}{_image_ext(b.paste_img)}"
            bp.write_bytes(b.paste_img)
            block_paths.append(str(bp))
        paths["blocks"] = block_paths

        if compose and self.source_image and any(b.paste_img for b in self.blocks):
            composed = compose_blocks(self.source_image, self.blocks)
            cp = out / f"{stem}_{lang}_composed.png"
            cp.write_bytes(composed)
            paths["composed"] = str(cp)

        json_path = out / f"{stem}_result.json"
        payload = self.to_dict(embed_images=embed_images)
        payload["files"] = paths
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        paths["json"] = str(json_path)
        return paths


# =============================================================================
# 工具函数
# =============================================================================
def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(value: Optional[str]) -> Optional[bytes]:
    """解码接口返回的 base64，兼容 data URI 前缀与空白字符。"""
    if not value:
        return None
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    value = "".join(value.split())
    if not value:
        return None
    value += "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value)
    except Exception:
        return None


def _image_ext(data: bytes) -> str:
    """按文件头判断图片后缀，接口只返回 png/jpg，兜底 .png。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".png"


def _parse_rect(value: Any) -> Optional[Tuple[int, int, int, int]]:
    """官方 rect 形如 "236,71,75,28"（x,y,w,h），也兼容 list/tuple。"""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
            nums = [int(float(p)) for p in parts]
        elif isinstance(value, (list, tuple)):
            nums = [int(float(p)) for p in value]
        else:
            return None
    except (TypeError, ValueError):
        return None
    if len(nums) < 4:
        return None
    return nums[0], nums[1], nums[2], nums[3]


def _parse_points(value: Any) -> List[Dict[str, float]]:
    points: List[Dict[str, float]] = []
    if not isinstance(value, (list, tuple)):
        return points
    for p in value:
        if isinstance(p, dict) and "x" in p and "y" in p:
            try:
                points.append({"x": float(p["x"]), "y": float(p["y"])})
            except (TypeError, ValueError):
                continue
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                points.append({"x": float(p[0]), "y": float(p[1])})
            except (TypeError, ValueError):
                continue
    return points


def _sanitize_raw(value: Any) -> Any:
    """去掉 raw 里的大段 base64，保证 JSON 可读。"""
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            if k == "paste_img" and isinstance(v, str):
                cleaned[k] = "<base64 omitted>"
            elif k == "content" and isinstance(v, str):
                cleaned[k] = "<base64 omitted>"
            else:
                cleaned[k] = _sanitize_raw(v)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_raw(v) for v in value]
    return value


def _read_image(path: str | os.PathLike) -> bytes:
    p = Path(path)
    if not p.is_file():
        raise InputError(f"图片不存在：{p}")
    data = p.read_bytes()
    if not data:
        raise InputError(f"图片为空：{p}")
    if _image_ext(data) not in SUPPORTED_IMAGE_EXTS:
        # 官方只保证 png/jpg，其它格式交给接口判断，但提示一下
        print(f"[警告] {p.name} 不是 png/jpg，接口可能返回 69007/55001", file=sys.stderr)
    return data


def _validate_options(
    source_lang: str,
    target_lang: str,
    paste: int,
    view_type: int,
    need_intervene: int,
    model_type: str,
) -> None:
    if source_lang not in SOURCE_LANGS:
        print(
            f"[警告] 源语言 {source_lang!r} 不在文档列表中，仍按原值请求",
            file=sys.stderr,
        )
    if target_lang == "auto":
        raise InputError("目标语言不能为 auto")
    if target_lang not in TARGET_LANGS:
        print(
            f"[警告] 目标语言 {target_lang!r} 不在文档列表中，仍按原值请求",
            file=sys.stderr,
        )
    if paste not in PASTE_MODES:
        raise InputError(f"paste 只能是 0/1/2/3，当前 {paste}")
    if view_type not in VIEW_TYPES:
        raise InputError(f"view_type 只能是 0/1，当前 {view_type}")
    if need_intervene not in (0, 1):
        raise InputError(f"need_intervene 只能是 0/1，当前 {need_intervene}")
    if model_type != "nmt":
        raise InputError(f"model_type 固定为 nmt，当前 {model_type}")


def compose_blocks(source_image: bytes, blocks: Iterable[Block]) -> bytes:
    """把 paste=2 的区块小图按 rect 拼回原图，得到整图效果（需要 Pillow）。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise PicTransError("--compose 需要 Pillow，请先 pip install pillow") from exc

    base = Image.open(io.BytesIO(source_image)).convert("RGBA")
    for b in blocks:
        if not b.paste_img or not b.rect:
            continue
        x, y, w, h = b.rect
        x, y = max(0, x), max(0, y)
        w, h = min(w, base.width - x), min(h, base.height - y)
        if w <= 0 or h <= 0:
            continue
        layer = Image.open(io.BytesIO(b.paste_img)).convert("RGBA")
        if layer.size != (w, h):
            layer = layer.resize((w, h), Image.LANCZOS)
        base.alpha_composite(layer, (x, y))

    buf = io.BytesIO()
    base.save(buf, format="PNG")
    return buf.getvalue()


# =============================================================================
# 核心：翻译器
# =============================================================================
class PictureTranslator:
    """百度图片翻译 V2.0 客户端。

    >>> t = PictureTranslator(appid="xxx", token="yyy")
    >>> res = t.translate_file("test.png", source_lang="zh", target_lang="en", paste=1)
    >>> res.dst
    """

    def __init__(
        self,
        appid: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = 60.0,
        retries: int = 2,
    ):
        self.appid = appid or os.getenv(ENV_APPID, "")
        self.token = token or os.getenv(ENV_TOKEN, "")
        self.timeout = timeout
        self.retries = max(0, int(retries))

    # ------------------------------------------------------------ 对外方法
    def translate_file(
        self,
        path: str | os.PathLike,
        *,
        source_lang: str = "auto",
        target_lang: str = "en",
        paste: int = 1,
        view_type: int = 1,
        need_intervene: int = 0,
        model_type: str = "nmt",
    ) -> TranslateResult:
        """翻译本地图片文件。"""
        return self.translate_bytes(
            _read_image(path),
            source_lang=source_lang,
            target_lang=target_lang,
            paste=paste,
            view_type=view_type,
            need_intervene=need_intervene,
            model_type=model_type,
        )

    def translate_bytes(
        self,
        data: bytes,
        *,
        source_lang: str = "auto",
        target_lang: str = "en",
        paste: int = 1,
        view_type: int = 1,
        need_intervene: int = 0,
        model_type: str = "nmt",
    ) -> TranslateResult:
        """翻译内存中的图片字节。"""
        if not data:
            raise InputError("图片数据为空")
        b64 = _b64encode(data)
        if len(b64) > MAX_B64_SIZE:
            raise InputError(
                f"图片 Base64 后 {len(b64) / 1024 / 1024:.2f}MB，超过 5MB 上限"
            )
        return self.translate_base64(
            b64,
            source_lang=source_lang,
            target_lang=target_lang,
            paste=paste,
            view_type=view_type,
            need_intervene=need_intervene,
            model_type=model_type,
            _source_image=data,
        )

    def translate_base64(
        self,
        content: str,
        *,
        source_lang: str = "auto",
        target_lang: str = "en",
        paste: int = 1,
        view_type: int = 1,
        need_intervene: int = 0,
        model_type: str = "nmt",
        _source_image: Optional[bytes] = None,
    ) -> TranslateResult:
        """直接传入已编码好的 Base64 字符串。"""
        self._ensure_credentials()
        _validate_options(
            source_lang, target_lang, paste, view_type, need_intervene, model_type
        )

        if content.startswith("data:"):
            _, _, content = content.partition(",")
        if len(content) > MAX_B64_SIZE:
            raise InputError(
                f"图片 Base64 后 {len(content) / 1024 / 1024:.2f}MB，超过 5MB 上限"
            )

        payload = {
            "from": source_lang,
            "to": target_lang,
            "appid": self.appid,
            "need_intervene": int(need_intervene),
            "paste": int(paste),
            "content": content,
            "view_type": int(view_type),
            "model_type": model_type,
        }
        request_meta = {
            "source_lang": source_lang,
            "target_lang": target_lang,
            "paste": int(paste),
            "view_type": int(view_type),
            "need_intervene": int(need_intervene),
            "model_type": model_type,
        }

        started = time.perf_counter()
        raw = self._post(payload)
        elapsed = time.perf_counter() - started
        return self._parse(raw, request_meta, _source_image, elapsed)

    # ------------------------------------------------------------ 内部实现
    def _ensure_credentials(self) -> None:
        if not self.appid or not self.token:
            raise ConfigError(
                "缺少 APPID/TOKEN。请用 --appid --token 传入，"
                f"或设置环境变量 {ENV_APPID} / {ENV_TOKEN}"
            )

    def _post(self, payload: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        last_error: Optional[Exception] = None

        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(
                    API_URL,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = NetworkError(f"HTTP 请求失败：{exc}")
                if attempt < self.retries:
                    _sleep_backoff(attempt)
                    continue
                raise last_error from exc

            # 网关类错误：可重试
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = NetworkError(
                    f"HTTP {resp.status_code}：{resp.text[:200]}"
                )
                if attempt < self.retries:
                    _sleep_backoff(attempt)
                    continue
                raise last_error

            try:
                data = resp.json()
            except ValueError as exc:
                raise NetworkError(
                    f"接口返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}"
                ) from exc

            code = _error_code(data)
            if code:
                api_error = APIError(code, str(data.get("error_msg", "")), data)
                if code in RETRYABLE_CODES and attempt < self.retries:
                    _sleep_backoff(attempt)
                    last_error = api_error
                    continue
                raise api_error

            if resp.status_code != 200:
                raise NetworkError(
                    f"HTTP {resp.status_code}：{resp.text[:200]}"
                )
            return data

        raise last_error or PicTransError("请求失败")

    def _parse(
        self,
        raw: dict,
        request_meta: dict,
        source_image: Optional[bytes],
        elapsed: float,
    ) -> TranslateResult:
        blocks: List[Block] = []
        contents = raw.get("contents")
        if isinstance(contents, dict):  # 兼容单对象返回
            contents = [contents]
        for i, item in enumerate(contents or [], 1):
            if not isinstance(item, dict):
                continue
            blocks.append(
                Block(
                    index=i,
                    src=str(item.get("src") or ""),
                    dst=str(item.get("dst") or ""),
                    rect=_parse_rect(item.get("rect")),
                    line_count=int(item.get("line_count") or 0),
                    points=_parse_points(item.get("points")),
                    paste_img=_b64decode(item.get("paste_img")),
                )
            )

        return TranslateResult(
            source_lang=str(raw.get("from") or request_meta["source_lang"]),
            target_lang=str(raw.get("to") or request_meta["target_lang"]),
            src=str(raw.get("src") or ""),
            dst=str(raw.get("dst") or ""),
            full_image=_b64decode(raw.get("paste_img")),
            blocks=blocks,
            request=request_meta,
            raw=raw,
            source_image=source_image,
            elapsed=elapsed,
        )


def _error_code(data: Any) -> Optional[str]:
    """成功响应没有 error_code；部分网关会返回字符串 "0"。"""
    if not isinstance(data, dict):
        return None
    if "error_code" not in data:
        return None
    code = data.get("error_code")
    if code is None:
        return None
    code = str(code).strip()
    return code if code and code not in ("0", "200") else None


def _sleep_backoff(attempt: int) -> None:
    time.sleep(min(0.5 * (2 ** attempt), 5.0))


# =============================================================================
# 便捷函数（外部代码最短调用方式）
# =============================================================================
def translate_image(
    path: str | os.PathLike,
    target_lang: str = "en",
    source_lang: str = "auto",
    *,
    paste: int = 1,
    view_type: int = 1,
    need_intervene: int = 0,
    model_type: str = "nmt",
    appid: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 60.0,
    retries: int = 2,
) -> TranslateResult:
    """翻译一张本地图片并返回结果对象（不自动落盘）。"""
    translator = PictureTranslator(
        appid=appid, token=token, timeout=timeout, retries=retries
    )
    return translator.translate_file(
        path,
        source_lang=source_lang,
        target_lang=target_lang,
        paste=paste,
        view_type=view_type,
        need_intervene=need_intervene,
        model_type=model_type,
    )


def save_result(
    result: TranslateResult,
    out_dir: str | os.PathLike,
    stem: str,
    *,
    compose: bool = False,
    embed_images: bool = False,
    save_text: bool = True,
) -> Dict[str, Optional[str]]:
    """便捷落盘入口，等价于 result.save(...)。"""
    return result.save(
        out_dir,
        stem,
        compose=compose,
        embed_images=embed_images,
        save_text=save_text,
    )


# =============================================================================
# 命令行
# =============================================================================
def _collect_inputs(items: Sequence[str]) -> List[Path]:
    """展开文件/目录（目录递归查找 png/jpg/jpeg），保持稳定排序。"""
    files: List[Path] = []
    for item in items:
        p = Path(item)
        if p.is_dir():
            found = [
                f for f in p.rglob("*")
                if f.is_file() and f.suffix.lower() in SUPPORTED_IMAGE_EXTS
            ]
            files.extend(sorted(found))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[跳过] 路径不存在：{p}", file=sys.stderr)
    # 去重且保持顺序
    seen, unique = set(), []
    for f in files:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _unique_stem(stem: str, used: set) -> str:
    name = stem
    n = 2
    while name in used:
        name = f"{stem}_{n}"
        n += 1
    used.add(name)
    return name


def _print_languages() -> None:
    print("源语言（from）：")
    for code in SOURCE_LANGS:
        print(f"  {code:<5} {LANG_NAMES.get(code, '')}")
    print("\n目标语言（to）：")
    for code in TARGET_LANGS:
        print(f"  {code:<5} {LANG_NAMES.get(code, '')}")
    print("\n提示：文档语种列表可能滞后，接口支持新语种时可直接传简写。")


def _print_modes() -> None:
    print("paste（贴合方案）：")
    for k, v in PASTE_MODES.items():
        print(f"  {k}  {v}")
    print("\nview_type（擦除精度）：")
    for k, v in VIEW_TYPES.items():
        print(f"  {k}  {v}")
    print("\nneed_intervene（术语干预）：")
    print("  0  关闭")
    print("  1  启用（使用控制台术语库，接口无 term 参数）")
    print("\nmodel_type：固定 nmt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="百度图片翻译 V2.0 一体化脚本（支持全部 paste / view_type 方案）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python baidu_pic_translate.py -i test.png -f zh -t en -p 1 -o output\n"
            "  python baidu_pic_translate.py -i imgs/ -t en -p 2 --compose -o output --workers 4\n"
            "  python baidu_pic_translate.py -i a.png -t jp -p 0 --show-text\n"
            "  python baidu_pic_translate.py --list-modes\n"
            "  python baidu_pic_translate.py --list-langs\n"
        ),
    )
    parser.add_argument(
        "-i", "--input", nargs="+", metavar="PATH",
        help="待翻译图片或目录（可多个；目录会递归查找 png/jpg/jpeg）",
    )
    parser.add_argument(
        "-f", "--from", dest="source_lang", default="auto",
        help="源语言，默认 auto（自动识别）",
    )
    parser.add_argument(
        "-t", "--to", dest="target_lang", default="en",
        help="译文语言，默认 en",
    )
    parser.add_argument(
        "-p", "--paste", type=int, choices=[0, 1, 2, 3], default=1,
        help="贴合类型：0 纯文本 / 1 整图 / 2 区块 / 3 透明底坐标，默认 1",
    )
    parser.add_argument(
        "-v", "--view-type", type=int, choices=[0, 1], default=1,
        help="显示类型：0 通用 / 1 高精，默认 1",
    )
    parser.add_argument(
        "--need-intervene", type=int, choices=[0, 1], default=0,
        help="术语干预：0 否 / 1 是，默认 0",
    )
    parser.add_argument(
        "--model-type", default="nmt", choices=["nmt"],
        help="模型类型，固定 nmt",
    )
    parser.add_argument(
        "-o", "--out", default="output",
        help="输出目录，默认 output",
    )
    parser.add_argument(
        "--appid", default=os.getenv(ENV_APPID),
        help=f"APPID（也可用环境变量 {ENV_APPID}）",
    )
    parser.add_argument(
        "--token", default=os.getenv(ENV_TOKEN),
        help=f"Access Token（也可用环境变量 {ENV_TOKEN}）",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="单次请求超时秒数，默认 60",
    )
    parser.add_argument(
        "--retries", type=int, default=2,
        help="失败重试次数（仅网络错误、5xx、53001、55004、59004），默认 2",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="批量并发数，默认 1（注意 QPS 限制，超限返回 59004）",
    )
    parser.add_argument(
        "--compose", action="store_true",
        help="paste=2 时用区块小图拼回整图（需要 Pillow）",
    )
    parser.add_argument(
        "--embed-images", action="store_true",
        help="把图片 base64 一并写入 result.json（文件会很大）",
    )
    parser.add_argument(
        "--no-text", action="store_true",
        help="不生成 *_text.txt",
    )
    parser.add_argument(
        "--show-text", action="store_true",
        help="在终端打印原文/译文",
    )
    parser.add_argument(
        "--list-langs", action="store_true",
        help="列出支持的语种后退出",
    )
    parser.add_argument(
        "--list-modes", action="store_true",
        help="列出全部参数方案后退出",
    )
    return parser


def _process_one(
    translator: PictureTranslator,
    path: Path,
    args: argparse.Namespace,
    stem: str,
) -> Tuple[Path, Optional[TranslateResult], Optional[Exception], Dict[str, Optional[str]], float]:
    started = time.perf_counter()
    try:
        result = translator.translate_file(
            path,
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            paste=args.paste,
            view_type=args.view_type,
            need_intervene=args.need_intervene,
            model_type=args.model_type,
        )
        files = result.save(
            args.out,
            stem,
            compose=args.compose,
            embed_images=args.embed_images,
            save_text=not args.no_text,
        )
        return path, result, None, files, time.perf_counter() - started
    except Exception as exc:  # 批量场景下单个失败不应中断整体
        return path, None, exc, {}, time.perf_counter() - started


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_langs:
        _print_languages()
        return 0
    if args.list_modes:
        _print_modes()
        return 0
    if not args.input:
        parser.error("缺少 -i/--input")

    if args.compose and args.paste != 2:
        print("[提示] --compose 只在 paste=2 时有意义，已忽略", file=sys.stderr)
        args.compose = False

    try:
        translator = PictureTranslator(
            appid=args.appid,
            token=args.token,
            timeout=args.timeout,
            retries=args.retries,
        )
        translator._ensure_credentials()
        _validate_options(
            args.source_lang,
            args.target_lang,
            args.paste,
            args.view_type,
            args.need_intervene,
            args.model_type,
        )
    except PicTransError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover

    files = _collect_inputs(args.input)
    if not files:
        print("没有找到可处理的图片", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(
        f"共 {len(files)} 张图片 | {args.source_lang} -> {args.target_lang} | "
        f"paste={args.paste} view_type={args.view_type} "
        f"need_intervene={args.need_intervene} | 输出 {out}"
    )

    used_stems: set = set()
    tasks = [(f, _unique_stem(f.stem, used_stems)) for f in files]
    succeeded, failed = 0, 0
    counter_lock = threading.Lock()
    started_all = time.perf_counter()

    def handle(item: Tuple[Path, str]) -> None:
        nonlocal succeeded, failed
        path, stem = item
        p, result, error, outputs, cost = _process_one(translator, path, args, stem)
        with counter_lock:
            if error is None and result is not None:
                succeeded += 1
            else:
                failed += 1
        if error is None and result is not None:
            print(f"[OK]   {p.name}  ({cost:.2f}s, {len(result.blocks)} 块)")
            for key in ("image", "composed", "json", "text"):
                if outputs.get(key):
                    print(f"       {outputs[key]}")
            if outputs.get("blocks"):
                print(f"       {len(outputs['blocks'])} 个区块小图")
            if args.show_text and result.dst:
                print(f"       原文: {result.src[:200]}")
                print(f"       译文: {result.dst[:200]}")
        else:
            print(f"[FAIL] {p.name}  {error}", file=sys.stderr)

    if args.workers > 1 and len(tasks) > 1:
        with ThreadPoolExecutor(max_workers=min(args.workers, len(tasks))) as pool:
            futures = {pool.submit(handle, item): item[0] for item in tasks}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # 理论上 handle 内部已捕获
                    failed += 1
                    print(f"[FAIL] {futures[future]}  {exc}", file=sys.stderr)
    else:
        for item in tasks:
            handle(item)

    total = time.perf_counter() - started_all
    print(f"\n完成：成功 {succeeded} / 失败 {failed}，总用时 {total:.2f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
