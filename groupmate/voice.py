# -*- coding: utf-8 -*-
"""语音识别:微信语音 → SILK → WAV → 文字。

链路(每一段都实测过):
  1. MediaDownloader.download_voice()  → .silk 二进制(带 aeskey 解密)
  2. pysilk.decode()                   → 16bit/24kHz 单声道 PCM
  3. wave 模块包成 WAV
  4. 在线语音识别(OpenAI 兼容接口,如 SiliconFlow 的 SenseVoice / Whisper)
  5. 失败时如return None,绝不抛异常打断主流程

为什么用在线识别:
  本地试过 SAPI(MS-2052-80-DESK)、OneCore DNN(zh-CN)、winsdk 现代 API,
  都因为虚拟机没有音频设备 / winsdk 投影 bug 而不可用。
  在线 Whisper/SenseVoice 中文准确率也明显更好。

设计原则:
  * 识别失败绝不影响文字回复
  * 结果缓存到磁盘,同一段语音不重复花钱
  * 没有 API Key 时如实说明,不静默假装成功
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
import urllib.error
import urllib.request
import wave
from typing import Optional

# 微信语音解码后的固定参数(SILK 内部就是 24kHz)
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2          # 16 bit
CHANNELS = 1

# 默认识别服务:SiliconFlow(OpenAI 兼容,有免费模型)
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"


class VoiceError(Exception):
    """语音处理失败(调用方应捕获,不要让它打断回复)。"""


# ---------------- SILK → WAV ----------------

def silk_to_wav(silk_path: str, wav_path: str) -> str:
    """把微信的 .silk 音频解码成标准 WAV(16bit 24kHz 单声道)。

    返回 wav_path。失败抛 VoiceError。
    """
    if not silk_path or not os.path.exists(silk_path):
        raise VoiceError("silk 文件不存在: %s" % silk_path)

    try:
        from pysilk import decode as _silk_decode
    except Exception as e:
        raise VoiceError(
            "缺少 SILK 解码器(pysilk)。安装: pip install silk-python") from e

    pcm_path = wav_path + ".pcm"
    try:
        # pysilk 要的是文件对象,不是路径(踩过这个坑)
        with open(silk_path, "rb") as fin, open(pcm_path, "wb") as fout:
            _silk_decode(fin, fout, SAMPLE_RATE)
        with open(pcm_path, "rb") as f:
            pcm = f.read()
        if not pcm:
            raise VoiceError("SILK 解码结果为空(可能不是有效语音)")

        os.makedirs(os.path.dirname(os.path.abspath(wav_path)) or ".", exist_ok=True)
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        return wav_path
    except VoiceError:
        raise
    except Exception as e:
        raise VoiceError("SILK 解码失败: %r" % (e,)) from e
    finally:
        try:
            if os.path.exists(pcm_path):
                os.remove(pcm_path)
        except OSError:
            pass


def wav_duration(path: str) -> float:
    """WAV 时长(秒)。读不到返回 0。"""
    try:
        with wave.open(path, "rb") as w:
            if w.getframerate():
                return w.getnframes() / float(w.getframerate())
    except Exception:
        pass
    return 0.0


# ---------------- 在线语音识别 ----------------

def _read_hkcu_env(name: str) -> str:
    """从 HKCU\\Environment 直接读环境变量。

    为什么需要这层:机器人可能从**旧会话**启动(比如刚用 reg 设了变量、
    但启动它的那个 cmd 会话还没继承到),于是一直观测到"没配 Key"。
    直接读注册表最可靠,不依赖会话继承。
    """
    if not name:
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            v, _ = winreg.QueryValueEx(k, name)
        return str(v or "")
    except Exception:
        return ""


class VoiceTranscriber:
    """把 WAV 发给在线语音识别服务,拿回文字。

    用 OpenAI 兼容的 /audio/transcriptions 接口(Whisper / SenseVoice 都支持)。
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 api_key: str = "", model: str = DEFAULT_MODEL,
                 api_key_env: str = "ASR_API_KEY",
                 timeout: float = 60.0, cache_dir: str = "",
                 language: str = "zh"):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        # Key 三级兜底:显式传入 -> 进程环境变量 -> HKCU 注册表
        self.api_key = (api_key or os.environ.get(api_key_env, "")
                        or _read_hkcu_env(api_key_env))
        self.model = model or DEFAULT_MODEL
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.language = language
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "_asr_cache")
        self._cache: dict = {}
        self._cache_ts: dict = {}       # key -> 写入时间,用于按时间裁剪
        self._cache_keep = 2000         # 内存/磁盘缓存上限
        self._load_cache()

    # ---------- 缓存 ----------

    def _cache_file(self) -> str:
        return os.path.join(self.cache_dir, "asr_cache.json")

    def _load_cache(self) -> None:
        try:
            with open(self._cache_file(), "r", encoding="utf-8") as f:
                self._cache = json.load(f)
        except Exception:
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = self._cache_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._cache_file())
        except Exception:
            pass

    @staticmethod
    def _key_for(path: str) -> str:
        """用文件内容算 key —— 同一段语音换名字也能命中缓存。"""
        try:
            h = hashlib.sha1()
            with open(path, "rb") as f:
                while True:
                    b = f.read(65536)
                    if not b:
                        break
                    h.update(b)
            return h.hexdigest()
        except Exception:
            return ""

    # ---------- 主流程 ----------

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def transcribe(self, wav_path: str, use_cache: bool = True) -> Optional[str]:
        """把 WAV 转成文字。失败返回 None(调用方照常走文字流程)。

        结果缓存到磁盘:同一段语音只识别一次,避免重复计费。
        没配 Key / 文件读不到这类"调用方配错了"的情况会抛 VoiceError,
        方便上层如实告诉用户;网络/服务端抖动返回 None(当成没听清)。
        """
        if not wav_path or not os.path.exists(wav_path):
            raise VoiceError("音频文件不存在: %s" % wav_path)

        key = self._key_for(wav_path)
        if use_cache and key and key in self._cache:
            return self._cache[key]

        if not self.api_key:
            raise VoiceError(
                "没有配语音识别 API Key(环境变量 %s)。"
                "去 https://cloud.siliconflow.cn 免费申请,然后设上即可。"
                % self.api_key_env)

        try:
            text = self._post(wav_path)
        except VoiceError:
            raise
        except Exception as e:
            raise VoiceError("语音识别请求失败: %r" % (e,)) from e

        if text and key:
            self._cache[key] = text                  # 纯 str,保持文件格式兼容
            self._cache_ts[key] = time.time()
            self.trim_cache()                        # 有上限,防止无限增长
            self._save_cache()
        return text or None

    def trim_cache(self, keep: int = 0) -> int:
        """把内存缓存裁到 keep 条(按写入时间,保留最新的)。

        ⚠ 为什么必须同时裁内存:只裁磁盘的话,下一次 _save_cache()
        会把内存里那份完整的又整份写回去 —— 磁盘清理完全白做。
        """
        keep = keep or self._cache_keep
        if len(self._cache) <= keep:
            return 0
        order = sorted(self._cache.keys(),
                       key=lambda k: self._cache_ts.get(k, 0.0), reverse=True)
        drop = order[keep:]
        for k in drop:
            self._cache.pop(k, None)
            self._cache_ts.pop(k, None)
        return len(drop)

    def _post(self, wav_path: str) -> str:
        """multipart/form-data 上传 WAV。"""
        boundary = "----VoiceBoundary%d" % int(time.time() * 1000)
        fname = os.path.basename(wav_path)
        with open(wav_path, "rb") as f:
            audio = f.read()

        parts = []
        # model
        parts.append(("--%s\r\n" % boundary).encode())
        parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
        parts.append(self.model.encode("utf-8") + b"\r\n")
        # language(部分服务需要)
        if self.language:
            parts.append(("--%s\r\n" % boundary).encode())
            parts.append(b'Content-Disposition: form-data; name="language"\r\n\r\n')
            parts.append(self.language.encode("utf-8") + b"\r\n")
        # file
        parts.append(("--%s\r\n" % boundary).encode())
        parts.append(('Content-Disposition: form-data; name="file"; '
                      'filename="%s"\r\n' % fname).encode())
        parts.append(b"Content-Type: audio/wav\r\n\r\n")
        parts.append(audio)
        parts.append(b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            self.base_url + "/audio/transcriptions", data=body, method="POST")
        req.add_header("Content-Type",
                       "multipart/form-data; boundary=%s" % boundary)
        req.add_header("Authorization", "Bearer " + self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise VoiceError("识别服务返回 %s: %s" % (e.code, detail))
        except Exception as e:
            raise VoiceError("连接识别服务失败: %r" % (e,)) from e

        # 兼容两种返回: {"text": "..."} 或 OpenAI 风格
        try:
            data = json.loads(raw)
        except Exception:
            return raw.strip()

        if isinstance(data, dict):
            for k in ("text", "result", "transcription"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            # 有些服务返回 segments
            segs = data.get("segments")
            if isinstance(segs, list):
                joined = "".join(str(s.get("text") or "") for s in segs
                                 if isinstance(s, dict))
                if joined.strip():
                    return joined.strip()
        return ""


# ---------------- 对外一步到位 ----------------

def transcribe_silk(silk_path: str, wav_dir: str,
                    transcriber: VoiceTranscriber) -> Optional[str]:
    """SILK 文件 → 文字(解码 + 识别一步到位)。失败返回 None。"""
    try:
        name = os.path.splitext(os.path.basename(silk_path))[0] + ".wav"
        os.makedirs(wav_dir, exist_ok=True)
        wav = os.path.join(wav_dir, name)
        silk_to_wav(silk_path, wav)
        return transcriber.transcribe(wav)
    except VoiceError:
        raise
    except Exception:
        return None


# ---------------- 磁盘清理 ----------------

def prune_dir(directory: str, keep: int = 300,
              suffix: tuple = (".wav", ".silk", ".pcm")) -> int:
    """把目录里最旧的文件删掉,只留最近 keep 个(按修改时间)。

    为什么需要:每来一条语音就会落盘 .silk + .wav,长期运行会把磁盘吃满。
    返回删除的文件数。
    """
    if not directory or not os.path.isdir(directory):
        return 0
    try:
        files = []
        for fn in os.listdir(directory):
            if suffix and not fn.lower().endswith(suffix):
                continue
            p = os.path.join(directory, fn)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                continue
        if len(files) <= keep:
            return 0
        files.sort(reverse=True)          # 新的在前
        removed = 0
        for _mt, p in files[keep:]:
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
        return removed
    except Exception:
        return 0


def prune_cache(cache_dir: str, keep: int = 2000) -> int:
    """把 ASR 结果缓存文件裁到 keep 条。返回删掉的条数。

    ⚠ 注意:这只改磁盘。内存里那份必须由 VoiceTranscriber.trim_cache()
    一起裁,否则下一次 _save_cache() 又会把完整的写回去。
    JSON 里没有时间信息,所以这里保留靠后的条目(近似"较新写入的")。
    """
    path = os.path.join(cache_dir, "asr_cache.json")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or len(data) <= keep:
            return 0
        items = list(data.items())
        kept = dict(items[-keep:])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return len(items) - len(kept)
    except Exception:
        return 0
