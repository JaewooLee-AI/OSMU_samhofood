"""첨부 이미지/동영상을 작업 폴더로 정규화하고, AI 분석·미리보기용 대표 프레임을 만드는 서비스"""
import os
import json
import shutil
import asyncio
import subprocess
from typing import List, Optional
from PIL import Image, ImageOps
from models.schema import MediaAsset

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic", ".heif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".3gp"}
ALLOWED_EXTENSIONS = sorted(e.lstrip(".") for e in IMAGE_EXTS | VIDEO_EXTS)

KEYFRAME_MAX = 1024        # AI 비전 입력용 대표 프레임 최대 변 길이(px)
VIDEO_KEYFRAMES = 4        # 동영상당 추출할 대표 프레임 수


def media_kind(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


class MediaLibrary:
    def __init__(self, root_dir: str):
        self.assets_dir = os.path.join(root_dir, "assets")
        self.frames_dir = os.path.join(self.assets_dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)

    @staticmethod
    def next_id(existing: List[MediaAsset], prefix: str) -> str:
        nums = [int(a.id[len(prefix):]) for a in existing if a.id.startswith(prefix) and a.id[len(prefix):].isdigit()]
        return f"{prefix}{max(nums, default=0) + 1}"

    async def ingest(self, src_path: str, asset_id: str, original_name: str = "", source: str = "user") -> MediaAsset:
        name = original_name or os.path.basename(src_path)
        kind = media_kind(name) or media_kind(src_path)
        if kind is None:
            raise ValueError(f"지원하지 않는 파일 형식입니다: {name}")
        if kind == "image":
            return await asyncio.to_thread(self._ingest_image, src_path, asset_id, name, source)
        return await self._ingest_video(src_path, asset_id, name, source)

    def remove(self, asset: MediaAsset):
        for p in [asset.path] + asset.keyframes:
            if p and os.path.exists(p) and os.path.dirname(os.path.abspath(p)).startswith(os.path.abspath(self.assets_dir)):
                os.remove(p)

    # ------------------------------------------------------------------
    def _ingest_image(self, src: str, asset_id: str, name: str, source: str) -> MediaAsset:
        try:
            img = Image.open(src)
            img = ImageOps.exif_transpose(img)  # 휴대폰 사진 회전 정보 반영
        except Exception:
            # HEIC 등 Pillow 미지원 포맷은 ffmpeg로 변환 시도
            tmp = os.path.join(self.assets_dir, f"{asset_id}_conv.png")
            res = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-frames:v", "1", tmp], capture_output=True)
            if res.returncode != 0:
                raise ValueError(f"이미지를 열 수 없습니다: {name}")
            img = Image.open(tmp)
            img.load()
            os.remove(tmp)

        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if has_alpha else "RGB")
        ext = ".png" if has_alpha else ".jpg"
        dest = os.path.join(self.assets_dir, f"{asset_id}{ext}")
        img.save(dest, quality=93) if ext == ".jpg" else img.save(dest)

        thumb = img.convert("RGB")
        thumb.thumbnail((KEYFRAME_MAX, KEYFRAME_MAX))
        kf = os.path.join(self.frames_dir, f"{asset_id}_kf0.jpg")
        thumb.save(kf, quality=85)
        return MediaAsset(id=asset_id, kind="image", source=source, path=dest, original_name=name,
                          width=img.width, height=img.height, keyframes=[kf], keyframe_times=[0.0])

    async def _ingest_video(self, src: str, asset_id: str, name: str, source: str) -> MediaAsset:
        ext = os.path.splitext(name)[1].lower() or ".mp4"
        dest = os.path.join(self.assets_dir, f"{asset_id}{ext}")
        if os.path.abspath(src) != os.path.abspath(dest):
            await asyncio.to_thread(shutil.copy2, src, dest)

        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", dest,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise ValueError(f"동영상을 읽을 수 없습니다: {name} ({err.decode(errors='ignore')[:120]})")
        info = json.loads(out.decode() or "{}")
        vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        if not vstreams:
            raise ValueError(f"영상 트랙이 없는 파일입니다: {name}")
        duration = float(info.get("format", {}).get("duration") or vstreams[0].get("duration") or 0)
        if duration <= 0:
            raise ValueError(f"동영상 길이를 확인할 수 없습니다: {name}")

        keyframes, times = [], []
        for i in range(VIDEO_KEYFRAMES):
            t = round(duration * (0.1 + 0.8 * i / max(1, VIDEO_KEYFRAMES - 1)), 2)
            kf = os.path.join(self.frames_dir, f"{asset_id}_kf{i}.jpg")
            p = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", dest, "-frames:v", "1",
                "-vf", f"scale='min({KEYFRAME_MAX},iw)':-2:force_original_aspect_ratio=decrease", "-q:v", "3", kf,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            await p.wait()
            if os.path.exists(kf):
                keyframes.append(kf)
                times.append(t)
        if not keyframes:
            raise ValueError(f"동영상 프레임을 추출하지 못했습니다: {name}")

        # 회전 메타데이터가 반영된 실제 표시 크기는 추출 프레임 기준으로 계산
        with Image.open(keyframes[0]) as im:
            kw, kh = im.size
        sw, sh = int(vstreams[0].get("width") or kw), int(vstreams[0].get("height") or kh)
        if (kw > kh) != (sw > sh):
            sw, sh = sh, sw
        return MediaAsset(id=asset_id, kind="video", source=source, path=dest, original_name=name,
                          width=sw, height=sh, duration=round(duration, 2),
                          keyframes=keyframes, keyframe_times=times)
