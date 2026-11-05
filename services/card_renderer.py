"""카드뉴스 렌더러 (1080x1350, 4:5) — 공통 엔진.

브랜드 표시(로고·색·소개·연락처·QR)는 스토리보드의 brand_id 브랜드 킷과 콘텐츠별 brand_elements 선택을 따른다.
레이아웃: 표지·본문·번호형·빅넘버·비교표·마무리(CTA)·1장 카드, 사진이 있으면 전면/상단 사진 레이아웃.
"""
import io
import re
import logging
import os
import asyncio
import subprocess
import tempfile
from typing import List, Optional, Tuple
from PIL import Image, ImageDraw, ImageOps
from jinja2 import Environment, FileSystemLoader, select_autoescape
from models.schema import CardSlide, EditableStoryboard, MediaAsset, BrandElements
from services.fonts import korean_font, display_font, wrap_text_px, text_height, use_fonts, reset_fonts
from services import brand_kit as bk
from services import design_templates
from services import emphasis
from services.fonts import wrap_text_balanced

logger = logging.getLogger(__name__)


def normalize_table(table: Optional[dict]) -> Tuple[List[str], List[List[str]]]:
    """비교표 dict({행 라벨: [값…]}, 첫 항목이 헤더)를 (헤더, 데이터 행)으로 정규화"""
    if not table or not isinstance(table, dict):
        return [], []
    items = []
    for key, values in table.items():
        vals = values if isinstance(values, list) else [values]
        items.append([str(key)] + [str(v) for v in vals])
    width = max(len(r) for r in items)
    items = [r + [""] * (width - len(r)) for r in items]
    return items[0], items[1:]


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    h = (hex_color or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def tint(hex_color: str, amount: float) -> str:
    """흰색과 섞어 연한 색 만들기 (amount=0.85면 85% 흰색)"""
    r, g, b = _rgb(hex_color)
    mix = lambda c: int(c + (255 - c) * amount)
    return "#{:02X}{:02X}{:02X}".format(mix(r), mix(g), mix(b))


def make_qr(elements: BrandElements) -> Optional[Image.Image]:
    """콘텐츠마다 입력한 QR: 올린 이미지가 있으면 그것, 없으면 주소로 즉석 생성"""
    try:
        if elements.qr_image and os.path.exists(elements.qr_image):
            return Image.open(elements.qr_image).convert("RGB")
        if elements.qr_url.strip():
            import qrcode
            qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(elements.qr_url.strip())
            qr.make(fit=True)
            return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception as e:
        logger.warning(f"QR 생성 실패: {e}")
    return None


class CardNewsRendererService:
    """고해상도 카드뉴스 (1080x1350) 자동 렌더링 서비스"""

    WIDTH = 1080
    HEIGHT = 1350
    MARGIN = 70

    TYPE_NAMES = {
        "cover": "핵심 브리핑",
        "body": "핵심 포인트",
        "numbered": "핵심 정리",
        "big_number": "숫자로 보기",
        "comparison_table": "한눈에 비교",
        "event_info": "행사·이벤트 안내",
        "call_to_action": "문의·신청 안내",
        "single": "한 장 요약",
    }

    def __init__(self, export_dir: str = None):
        if not export_dir:
            from services.paths import EXPORTS_DIR
            self.export_dir = os.path.join(EXPORTS_DIR, "cards")
        else:
            self.export_dir = export_dir
        os.makedirs(self.export_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 진입점
    # ------------------------------------------------------------------
    def _kit(self, sb: EditableStoryboard) -> bk.BrandKit:
        try:
            return bk.load_kit(sb.brand_id) if sb.brand_id else bk.active_kit()
        except (OSError, ValueError):
            return bk.active_kit()

    def slide_path(self, index: int) -> str:
        return os.path.join(self.export_dir, f"slide_{index + 1:02d}.png")

    async def render_all_slides(self, storyboard: EditableStoryboard) -> List[str]:
        """스토리보드의 모든 카드뉴스 슬라이드를 고화질 PNG로 렌더링"""
        self._remove_stale_outputs(len(storyboard.card_slides))
        paths = [await self.render_slide(storyboard, i) for i in range(len(storyboard.card_slides))]
        await asyncio.to_thread(self._render_html_bundle, storyboard)
        return paths

    async def render_slide(self, storyboard: EditableStoryboard, index: int) -> str:
        """카드 한 장만 다시 그리기 (카드별 수정 후 즉시 반영용)"""
        kit = self._kit(storyboard)
        slide = storyboard.card_slides[index]
        total = len(storyboard.card_slides)
        out_path = self.slide_path(index)
        photo = None
        asset = storyboard.asset(slide.asset_id)
        if asset and slide.slide_type not in ("comparison_table", "big_number", "event_info"):
            photo = await asyncio.to_thread(self._load_asset_image, asset)
        ctx = _Ctx(kit, storyboard.brand_elements, index, total, slide, design_templates.get(storyboard.design_id))
        img = await asyncio.to_thread(self._draw, ctx, photo)
        img.save(out_path, "PNG")
        return out_path

    def _remove_stale_outputs(self, count: int):
        """이전 렌더링이 더 많은 장수였다면 남은 파일 정리 (예: 8장 → 7장)"""
        for name in os.listdir(self.export_dir):
            if name.startswith("slide_") and name.endswith((".png", ".html")):
                try:
                    idx = int(name[6:8])
                except ValueError:
                    continue
                if idx > count:
                    os.remove(os.path.join(self.export_dir, name))

    # ------------------------------------------------------------------
    # 에셋 이미지 로드 (동영상은 원본 해상도로 대표 프레임 추출)
    # ------------------------------------------------------------------
    def _load_asset_image(self, asset: MediaAsset) -> Optional[Image.Image]:
        try:
            if asset.kind == "image":
                with Image.open(asset.path) as im:
                    return ImageOps.exif_transpose(im).convert("RGB")
            times = asset.keyframe_times or [asset.duration / 2]
            t = times[len(times) // 2]
            with tempfile.TemporaryDirectory() as tmp:
                frame = os.path.join(tmp, "frame.png")
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", asset.path, "-frames:v", "1", frame],
                               check=True, capture_output=True)
                with Image.open(frame) as im:
                    return im.convert("RGB")
        except Exception as e:
            logger.warning(f"에셋 로드 실패 {asset.id}: {e}")
            return None

    @staticmethod
    def _cover_fit(img: Image.Image, w: int, h: int) -> Image.Image:
        """비율을 유지하며 영역을 꽉 채우도록 자르기 (인물 얼굴이 잘리지 않게 약간 위쪽 기준)"""
        return ImageOps.fit(img, (w, h), method=Image.LANCZOS, centering=(0.5, 0.42))

    # ------------------------------------------------------------------
    # 레이아웃 분기
    # ------------------------------------------------------------------
    def _draw(self, ctx: "_Ctx", photo: Optional[Image.Image]) -> Image.Image:
        """카드 한 장 그리기. 디자인 템플릿의 제목·본문 글꼴을 이 그리기 동안만 적용"""
        token = use_fonts(ctx.design.heading_font, ctx.design.body_font)
        try:
            clean, terms = emphasis.parse(ctx.slide.lead_headline)   # *강조어* → 별표 지우고 강조어 기억
            body = (ctx.slide.body_content or "").replace("\\n", "\n")  # 글자 '\n'이 그대로 찍히지 않게
            if clean != ctx.slide.lead_headline or body != (ctx.slide.body_content or ""):
                ctx = _Ctx(ctx.kit, ctx.el, ctx.index, ctx.total, ctx.slide.model_copy(update={"lead_headline": clean, "body_content": body}), ctx.design)
            if ctx.design.heading_font or ctx.design.body_font:
                ctx = self._sanitized(ctx)
                from services.fonts import sanitize_text
                terms = [sanitize_text(t, ctx.design.heading_font, ctx.design.body_font) for t in terms]
            ctx.emph = terms
            return self._draw_card(ctx, photo)
        finally:
            reset_fonts(token)

    @staticmethod
    def _sanitized(ctx: "_Ctx") -> "_Ctx":
        """선택한 글꼴에 없는 기호(·, … 등)를 비슷한 글자로 바꾼 복사본 (원본 스토리보드는 그대로)"""
        from services.fonts import sanitize_text
        h, b = ctx.design.heading_font, ctx.design.body_font
        fix = lambda t: sanitize_text(t, h, b)
        s = ctx.slide
        slide = s.model_copy(update={
            "lead_headline": fix(s.lead_headline), "body_content": fix(s.body_content), "takeaway": fix(s.takeaway),
            "badge": fix(s.badge), "stat_value": fix(s.stat_value), "stat_label": fix(s.stat_label),
            "comparison_table": ({fix(k): [fix(str(v)) for v in (vals if isinstance(vals, list) else [vals])] for k, vals in s.comparison_table.items()}
                                 if s.comparison_table else s.comparison_table)})
        kit = ctx.kit.model_copy(update={"brand_name": fix(ctx.kit.brand_name), "tagline": fix(ctx.kit.tagline), "footer_text": fix(ctx.kit.footer_text)})
        return _Ctx(kit, ctx.el, ctx.index, ctx.total, slide, ctx.design)

    def _draw_card(self, ctx: "_Ctx", photo: Optional[Image.Image]) -> Image.Image:
        t = ctx.slide.slide_type
        if ctx.design.layout == "gov":
            return self._layout_gov(ctx, photo)
        if ctx.design.layout == "typo":
            if photo is not None and t not in ("comparison_table", "big_number", "event_info"):
                return self._layout_typo_photo(ctx, photo)   # 첨부 사진을 배경으로 (AI 그림 비용 0원)
            return self._layout_typo(ctx)
        if ctx.design.layout == "poster" and t == "cover":
            return self._layout_poster_cover(ctx, photo)
        if ctx.design.layout in ("color_block", "poster") and t in ("cover", "body", "numbered", "single", "event_info", "big_number", "call_to_action", "comparison_table"):
            return self._layout_color_block(ctx, photo)
        if ctx.design.layout == "illustrated" and t in ("cover", "body", "numbered", "single"):
            return self._layout_illustrated(ctx, photo)  # 한 장 카드(single)도 템플릿 모양으로 그림
        if t == "single":
            return self._layout_single(ctx, photo)
        if photo is not None and t in ("cover", "call_to_action"):
            return self._layout_full_photo(ctx, photo)
        img = Image.new("RGB", (self.WIDTH, self.HEIGHT), ctx.c.background)
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, self.WIDTH, 20], fill=ctx.c.primary)
        self._header(img, draw, ctx, dark=False)
        if t == "big_number":
            self._layout_big_number(draw, ctx)
        elif t == "event_info":
            self._layout_event_info(draw, ctx)
        elif t == "call_to_action":
            self._layout_cta(img, draw, ctx)
        elif photo is not None:
            self._layout_top_photo(img, draw, ctx, photo)
        else:
            self._layout_text(draw, ctx)
        self._takeaway_bar(draw, ctx, dark=False)
        self._footer(draw, ctx, dark=False)
        return img

    # ------------------------------------------------------------------
    # 공통 요소: 헤더(로고·소개·쪽수), 요약 바, 푸터(홈페이지·전화·SNS)
    # ------------------------------------------------------------------
    def _logo_visible(self, ctx: "_Ctx") -> bool:
        mode = ctx.el.logo
        if mode == "none":
            return False
        if mode == "cover_cta":
            return ctx.slide.slide_type in ("cover", "call_to_action", "single")
        return True

    def _paste_logo(self, img: Image.Image, ctx: "_Ctx", x: int, y: int, height: int, dark: bool) -> int:
        """로고 이미지(없으면 브랜드명 배지)를 그리고 폭을 반환"""
        if ctx.kit.logo_path:
            with Image.open(ctx.kit.logo_path) as im:
                logo = im.convert("RGBA")
            w = int(logo.width * height / max(1, logo.height))
            if w > 340:
                w, height = 340, int(logo.height * 340 / logo.width)
            logo = logo.resize((max(1, w), max(1, height)), Image.LANCZOS)
            if dark:  # 어두운 사진 위에서는 로고 뒤에 흰 배경판
                plate = Image.new("RGBA", (w + 24, height + 16), (255, 255, 255, 235))
                img.paste(plate, (x - 12, y - 8), plate)
            img.paste(logo, (x, y), logo)
            return w
        draw = ImageDraw.Draw(img)
        name = ctx.kit.brand_name
        return self._pill(draw, (x, y), name, display_font(26, name), ctx.c.primary, "#FFFFFF", pad_x=22, height=52, radius=10)

    def _header(self, img, draw, ctx: "_Ctx", dark: bool):
        W, M = self.WIDTH, self.MARGIN
        f_small = korean_font(21, "semibold")
        x = M
        if self._logo_visible(ctx):
            x += self._paste_logo(img, ctx, M, 60, 52, dark) + 18
        if ctx.kit.tagline:
            draw.text((x, 86), ctx.kit.tagline, font=f_small, fill="#E2E8F0" if dark else "#475569", anchor="lm")
        if ctx.total > 1:
            badge = f"{ctx.index + 1} / {ctx.total}"
            bw = int(f_small.getlength(badge)) + 44
            self._pill(draw, (W - M - bw, 60), badge, f_small, "#FFFFFF" if dark else tint(ctx.c.secondary, 0.92),
                       ctx.c.primary if dark else ctx.c.secondary, pad_x=22, height=50, radius=25,
                       outline=None if dark else tint(ctx.c.secondary, 0.7))
        if not dark:
            draw.line([M, 135, W - M, 135], fill="#E2E8F0", width=2)

    def _takeaway_bar(self, draw, ctx: "_Ctx", dark: bool, top: int = 1080, bottom: int = 1190):
        W, M = self.WIDTH, self.MARGIN
        bar = (M, top, W - M, bottom)
        draw.rounded_rectangle(bar, radius=18, fill="#FFFFFF" if dark else ctx.c.primary)
        cx, cy = M + 52, (bar[1] + bar[3]) // 2
        draw.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=ctx.c.accent)
        draw.text((cx, cy), "!", font=korean_font(28, "bold"), fill=ctx.c.primary, anchor="mm")
        f_take, take_lines = self._fit_lines(ctx.slide.takeaway, bar[2] - bar[0] - 130, start=28, minimum=20, max_lines=2, weight="bold")
        t_lh = int(text_height(f_take) * 1.15)
        t_y = cy - (t_lh * len(take_lines)) // 2
        for line in take_lines:
            draw.text((M + 100, t_y), line, font=f_take, fill=ctx.c.primary if dark else "#FFFFFF")
            t_y += t_lh

    def _footer(self, draw, ctx: "_Ctx", dark: bool, y: int = 1270):
        W, M = self.WIDTH, self.MARGIN
        left_parts = []
        if ctx.el.show_homepage:
            left_parts.append(ctx.kit.footer_text or ctx.kit.homepage_short)
        if ctx.el.show_sns and ctx.kit.sns_handle:
            left_parts.append(ctx.kit.sns_handle)
        left = "  |  ".join(p for p in left_parts if p)
        right = f"상담 {ctx.kit.phone}" if ctx.el.show_phone and ctx.kit.phone else ""
        if not left and not right:
            return
        draw.line([M, y - 45, W - M, y - 45], fill="#94A3B8" if dark else "#E2E8F0", width=2)
        if left:
            draw.text((M, y), left, font=korean_font(22, "medium"), fill="#E2E8F0" if dark else "#64748B", anchor="lm")
        if right:
            draw.text((W - M, y), right, font=display_font(24, right), fill=ctx.c.accent if dark else ctx.c.secondary, anchor="rm")

    def _tag(self, draw, ctx: "_Ctx", xy, dark: bool = False, height: int = 50):
        text = self.TYPE_NAMES.get(ctx.slide.slide_type, "핵심 포인트")
        if dark:
            return self._pill(draw, xy, text, display_font(21, text), ctx.c.accent, "#0F172A", pad_x=24, height=height, radius=height // 2)
        return self._pill(draw, xy, text, display_font(21, text), tint(ctx.c.secondary, 0.85), ctx.c.primary,
                          pad_x=24, height=height, radius=height // 2)

    # ------------------------------------------------------------------
    # 레이아웃
    # ------------------------------------------------------------------
    def _headline(self, draw, ctx, y: int, start=50, minimum=34, max_lines=3, color=None) -> int:
        W, M = self.WIDTH, self.MARGIN
        f_lead, lines = self._fit_lines(ctx.slide.lead_headline, W - 2 * M, start=start, minimum=minimum, max_lines=max_lines, weight="bold")
        lh = int(text_height(f_lead) * 1.18)
        for line in lines:
            draw.text((M, y), line, font=f_lead, fill=color or ctx.c.primary)
            y += lh
        return y

    def _layout_text(self, draw, ctx: "_Ctx"):
        W, M = self.WIDTH, self.MARGIN
        x = M
        if ctx.slide.slide_type == "numbered" and ctx.slide.badge:
            # 번호형: 큰 번호 원 + 태그
            draw.ellipse([M, 160, M + 80, 240], fill=ctx.c.accent)
            draw.text((M + 40, 200), ctx.slide.badge, font=display_font(34, ctx.slide.badge), fill=ctx.c.primary, anchor="mm")
            x = M + 100
        self._tag(draw, ctx, (x, 175))
        y = self._headline(draw, ctx, 265)
        card_top, card_bottom = max(y + 30, 430), 1050
        header, rows = normalize_table(ctx.slide.comparison_table)
        body = ctx.slide.body_content or ctx.slide.lead_headline
        if not (header and rows):
            # 글머리표가 적으면 상자를 내용 높이에 맞춰 남는 공간 가운데에 (빈 상자가 커 보이지 않도록)
            need = self._bullets_height(body, W - 2 * M - 72) + 72
            box_h = max(240, min(card_bottom - card_top, need))
            card_top += (card_bottom - card_top - box_h) // 2
            card_bottom = card_top + box_h
        draw.rounded_rectangle([M, card_top, W - M, card_bottom], radius=24, fill="#FFFFFF", outline="#CBD5E1", width=2)
        inner = (M + 36, card_top + 36, W - M - 36, card_bottom - 36)
        if header and rows:
            self._draw_table(draw, header, rows, inner, ctx)
        else:
            self._draw_bullets(draw, body, inner, ctx)

    def _layout_top_photo(self, img, draw, ctx: "_Ctx", photo: Image.Image):
        W, M = self.WIDTH, self.MARGIN
        pw, ph = W - 2 * M, 500
        pic = self._cover_fit(photo, pw, ph)
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw, ph], radius=24, fill=255)
        img.paste(pic, (M, 150), mask)
        x = M
        if ctx.slide.slide_type == "numbered" and ctx.slide.badge:
            draw.ellipse([M + 20, 170, M + 100, 250], fill=ctx.c.accent)
            draw.text((M + 60, 210), ctx.slide.badge, font=display_font(34, ctx.slide.badge), fill=ctx.c.primary, anchor="mm")
        self._tag(draw, ctx, (x, 675), height=46)
        y = self._headline(draw, ctx, 740, start=44, minimum=32, max_lines=2)
        self._draw_bullets(draw, ctx.slide.body_content, (M + 6, y + 18, W - M, 1055), ctx)

    # ------------------------------------------------------------------
    # 일러스트형: 제목 → 큰 그림 → 항목 박스 (표지는 그림을 더 크게)
    # ------------------------------------------------------------------
    def _illustration(self, img: Image.Image, ctx: "_Ctx", box, photo: Optional[Image.Image]):
        """그림 영역: 사진·AI 그림이 있으면 채우고, 없으면 브랜드 색 도형 장식"""
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        r = ctx.design.radius
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w, h], radius=r, fill=255)
        if photo is not None:
            img.paste(self._cover_fit(photo, w, h), (x0, y0), mask)
            return
        panel = Image.new("RGB", (w, h), tint(ctx.c.primary, 0.9))
        pd = ImageDraw.Draw(panel)
        pd.ellipse([-w * 0.1, h * 0.35, w * 0.45, h * 1.2], fill=tint(ctx.c.secondary, 0.78))
        pd.ellipse([w * 0.5, -h * 0.25, w * 1.1, h * 0.55], fill=tint(ctx.c.accent, 0.6))
        pd.ellipse([w * 0.3, h * 0.2, w * 0.7, h * 0.8], fill=tint(ctx.c.primary, 0.82))
        label = "" if ctx.slide.slide_type == "cover" else (ctx.slide.badge or str(ctx.index + 1))
        if label:
            pd.text((w // 2, h // 2), label, font=display_font(int(h * 0.38), label), fill=ctx.c.primary, anchor="mm")
        img.paste(panel, (x0, y0), mask)

    def _layout_illustrated(self, ctx: "_Ctx", photo: Optional[Image.Image]) -> Image.Image:
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        d = ctx.design
        img = Image.new("RGB", (W, H), ctx.c.background)
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, W, 20], fill=ctx.c.primary)
        self._header(img, draw, ctx, dark=False)
        cover = ctx.slide.slide_type == "cover"
        self._tag(draw, ctx, (M, 170))
        y = self._headline(draw, ctx, 250, start=54 if cover else 46, minimum=34, max_lines=3 if cover else 2)
        lines = [l.strip() for l in (ctx.slide.body_content or "").splitlines() if l.strip()]
        n = min(3, len(lines))
        # 그림 영역 높이: 표지는 크게, 본문은 템플릿 설정(항목이 적으면 더 크게)
        bottom = 1050
        ill_h = d.illustration_height + (140 if cover else 0) + (90 if n <= 1 else 0)
        ill_h = min(ill_h, bottom - (y + 24) - (0 if n == 0 else 110 * n + 14 * (n - 1) + 24))
        ill_h = max(260, ill_h)
        if n == 0:
            ill_h = bottom - (y + 24)  # 항목이 없으면 그림이 남는 공간을 채움
        top = y + 24
        self._illustration(img, ctx, (M, top, W - M, top + ill_h), photo)
        # 항목 박스
        area_top = top + ill_h + 24
        if n:
            gap = 14
            box_h = min(150, (bottom - area_top - gap * (n - 1)) // n)
            size = 30
            while size > 20:
                f = korean_font(size, "semibold")
                lh = int(text_height(f) * 1.3)
                tw = W - 2 * M - (150 if d.show_numbers else 70)
                if all(len(wrap_text_px(t, f, tw)) * lh + 28 <= box_h for t in lines[:n]):
                    break
                size -= 2
            f = korean_font(size, "semibold")
            lh = int(text_height(f) * 1.3)
            tw = W - 2 * M - (150 if d.show_numbers else 70)
            for i, t in enumerate(lines[:n]):
                by = area_top + i * (box_h + gap)
                fill = tint(ctx.c.primary, 0.92) if d.box_style == "filled" else "#FFFFFF"
                outline = None if d.box_style == "filled" else tint(ctx.c.primary, 0.55)
                draw.rounded_rectangle([M, by, W - M, by + box_h], radius=min(d.radius, box_h // 2), fill=fill, outline=outline, width=3)
                tx = M + 34
                if d.show_numbers:
                    cy = by + box_h // 2
                    draw.ellipse([M + 26, cy - 34, M + 94, cy + 34], fill=ctx.c.accent)
                    draw.text((M + 60, cy), f"{i + 1}", font=display_font(32, "0"), fill=ctx.c.primary, anchor="mm")
                    tx = M + 116
                wrapped = wrap_text_px(t, f, tw)[:3]
                ty = by + (box_h - len(wrapped) * lh) // 2
                for line in wrapped:
                    draw.text((tx, ty), line, font=f, fill="#1E293B")
                    ty += lh
        self._takeaway_bar(draw, ctx, dark=False)
        self._footer(draw, ctx, dark=False)
        return img

    # ------------------------------------------------------------------
    # 색 배경 일정표형: 위쪽 밝은 제목 영역 + 아래 꽉 찬 색 면에 줄 목록
    # ------------------------------------------------------------------
    @staticmethod
    def _lum(hex_color: str) -> float:
        r, g, b = (c / 255 for c in _rgb(hex_color))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def _on_color(self, fill: str, dark: str) -> str:
        """색 면 위 글자색: 흰색 대비가 충분하면 흰색, 아니면 어두운 색"""
        return "#FFFFFF" if 1.05 / (self._lum(fill) + 0.05) >= 3.2 else dark

    # ------------------------------------------------------------------
    # 타이포그래피형: 사진·그림 없이 큰 글씨·형광펜 강조·여백으로 전달 (AI 그림 비용 0원)
    # ------------------------------------------------------------------
    def _fit_title(self, text: str, width: int, start: int, minimum: int, max_lines: int):
        """균형 줄바꿈으로 제목 크기 맞추기"""
        size = start
        while True:
            font = display_font(size, text)
            lines = wrap_text_balanced(text or "", font, width)
            if len(lines) <= max_lines or size <= minimum:
                if len(lines) > max_lines:
                    lines = lines[:max_lines]
                    lines[-1] = lines[-1].rstrip() + "…"
                return font, lines
            size -= 2

    def _emph_title(self, draw, ctx: "_Ctx", y: int, start: int, minimum: int, max_lines: int, x: Optional[int] = None,
                    width: Optional[int] = None, color: Optional[str] = None, line_gap: float = 1.22, measure: bool = False,
                    emph_color: Optional[str] = None, center: bool = False) -> int:
        """제목을 그리고 강조어 아래쪽 절반을 형광펜으로 칠함. 강조 표기가 없으면 수치를 자동 강조"""
        W, M = self.WIDTH, self.MARGIN
        x = M if x is None else x
        width = (W - 2 * M) if width is None else width
        text = ctx.slide.lead_headline or ""
        terms = ctx.emph or emphasis.auto_terms(text)
        for term in terms:   # 강조어가 두 줄로 갈라지지 않게 강조어 안의 띄어쓰기를 붙임표 없는 공백으로
            if len(term) <= 14:
                text = text.replace(term, term.replace(" ", "\u00a0"), 1)
        terms = [term.replace(" ", "\u00a0") if len(term) <= 14 else term for term in terms]
        font, lines = self._fit_title(text, width, start, minimum, max_lines)
        if measure:
            return y + len(lines) * int(text_height(font) * line_gap)
        marks = emphasis.spans(text, terms)
        lh = int(text_height(font) * line_gap)
        hl = tint(ctx.c.accent, 0.35)
        x_base = x
        cursor = 0
        for line in lines:
            pos = text.find(line.rstrip("…"), cursor)
            spans_in_line = []
            if pos >= 0:
                cursor = pos + len(line)
                for a, b in marks:
                    s0, s1 = max(a, pos), min(b, pos + len(line))
                    if s0 < s1:
                        spans_in_line.append((s0 - pos, s1 - pos))
            plain = line.replace("\u00a0", " ")
            if center:
                x = x_base + (width - font.getlength(plain)) / 2
            if not emph_color:   # 밝은 바탕: 형광펜
                for a, b in spans_in_line:
                    x0, x1 = x + font.getlength(plain[:a]), x + font.getlength(plain[:b])
                    draw.rectangle([x0 - 4, y + int(lh * 0.48), x1 + 4, y + int(lh * 0.86)], fill=hl)
            draw.text((x, y), plain, font=font, fill=color or ctx.c.primary)
            if emph_color:       # 사진 위: 강조어만 포인트 색 글씨
                for a, b in spans_in_line:
                    draw.text((x + font.getlength(plain[:a]), y), plain[a:b], font=font, fill=emph_color)
            y += lh
        return y

    def _typo_paragraphs(self, draw, ctx: "_Ctx", lines: List[str], box, start: int = 38, minimum: int = 26, measure: bool = False,
                         color: str = "#334155", bar: Optional[str] = None) -> int:
        """본문 줄: 왼쪽 짧은 색 막대 + 넉넉한 줄 간격, 상자에 맞을 때까지 글자 크기 축소"""
        x0, y0, x1, y1 = box
        lines = [l for l in lines if l.strip()][:5]
        if not lines:
            return 0
        size = start
        while True:
            f = korean_font(size, "medium")
            lh = int(text_height(f) * 1.45)
            wrapped = [wrap_text_px(l, f, x1 - x0 - 34) for l in lines]
            gap = int(size * 0.9)
            need = sum(len(w) * lh for w in wrapped) + gap * (len(lines) - 1)
            if need <= y1 - y0 or size <= minimum:
                break
            size -= 2
        if measure:
            return min(need, y1 - y0)
        y = y0
        for w in wrapped:
            draw.rectangle([x0, y + int(lh * 0.22), x0 + 8, y + int(lh * 0.78)], fill=bar or ctx.c.secondary)
            for wl in w:
                if y + lh > y1 + 10:
                    return y - y0
                draw.text((x0 + 34, y), wl, font=f, fill=color)
                y += lh
            y += gap
        return y - y0

    def _typo_takeaway(self, draw, ctx: "_Ctx", y: int = 1150, color: Optional[str] = None, arrow: Optional[str] = None) -> None:
        M, W = self.MARGIN, self.WIDTH
        if not (ctx.slide.takeaway or "").strip():
            return
        f, lines = self._fit_lines(ctx.slide.takeaway, W - 2 * M - 60, start=32, minimum=24, max_lines=1, weight="bold")
        draw.text((M, y), "→", font=display_font(32, "→"), fill=arrow or ctx.c.secondary, anchor="lm")
        draw.text((M + 52, y), lines[0], font=f, fill=color or ctx.c.primary, anchor="lm")

    # ------------------------------------------------------------------
    # 공공 안내형: 하늘색 바탕 + 흰 패널 + 번호·소제목 알약 + 두 색 제목 + 줄 목록(라벨 칸·화살표·체크·날짜 알약) + 남색 결론 띠
    # (공공기관 안내 카드뉴스에서 흔한 구성을 참고해 새로 그린 모양. 사진은 첨부했을 때만 표지에 사용, AI 그림 0원)
    # ------------------------------------------------------------------
    DATE_TAIL_RE = re.compile(r"\s*[\(\[]?(\d{1,4}[./]\s?\d{1,2}\.?(?:\s?\([월화수목금토일]\))?(?:\s?[~\-–]\s?\d{1,4}[./]?\s?\d{0,2}\.?(?:\s?\([월화수목금토일]\))?)?)[\)\]]?\s*$")

    def _gov_rows(self, draw, ctx: "_Ctx", lines: List[str], box) -> None:
        x0, y0, x1, y1 = box
        c = ctx.c
        rows = []
        for line in lines[:6]:
            kind, label, value, date = "check", "", line, ""
            if "→" in line or "->" in line:
                kind = "arrow"
                label, value = [v.strip() for v in re.split(r"→|->", line, maxsplit=1)]
                m = re.match(r"^\s*([^:：]{1,8})\s*[:：]\s*(.+)$", label)
                if m:   # '항목: 이전 → 이후'
                    kind, label = "arrow3", m.group(1).strip()
                    value = (m.group(2).strip(), value)
            else:
                m = re.match(r"^\s*([^:：]{1,8})\s*[:：]\s*(.+)$", line)
                if m:
                    kind, label, value = "label", m.group(1).strip(), m.group(2).strip()
            if isinstance(value, str):
                dm = self.DATE_TAIL_RE.search(value)
                if dm and len(value) - len(dm.group(0)) >= 4:
                    date, value = dm.group(1).strip(), value[:dm.start()].strip()
            rows.append((kind, label, value, date))
        if not rows:
            return
        n = len(rows)
        row_h = min(170, (y1 - y0) // n)
        line_col = tint(c.primary, 0.82)
        has_label = any(r[0] in ("label", "arrow3") for r in rows)
        label_w = 190 if has_label else 0
        for i, (kind, label, value, date) in enumerate(rows):
            ry = y0 + i * row_h
            cy = ry + row_h // 2
            if i:
                draw.line([x0, ry, x1, ry], fill=line_col, width=2)
            x = x0
            if kind in ("label", "arrow3"):
                f_l, ll = self._fit_lines(label, label_w - 30, start=32, minimum=22, max_lines=2, weight="bold")
                lh = int(text_height(f_l) * 1.2)
                ty = cy - len(ll) * lh // 2
                for l in ll:
                    draw.text((x + (label_w - 20) / 2, ty), l, font=f_l, fill=c.primary, anchor="ma")
                    ty += lh
                draw.line([x0 + label_w, ry + 18, x0 + label_w, ry + row_h - 18], fill=line_col, width=2)
                x = x0 + label_w + 26
            elif kind == "check":
                draw.ellipse([x, cy - 17, x + 34, cy + 17], fill=c.secondary)
                draw.line([x + 9, cy, x + 15, cy + 7, x + 26, cy - 7], fill="#FFFFFF", width=5, joint="curve")
                x += 52
            right = x1
            if date:
                f_d = korean_font(22, "bold")
                dw = int(f_d.getlength(date)) + 36
                draw.rounded_rectangle([x1 - dw, cy - 22, x1, cy + 22], radius=22, fill=c.secondary)
                draw.text((x1 - dw / 2, cy), date, font=f_d, fill="#FFFFFF", anchor="mm")
                right = x1 - dw - 16
            if kind in ("arrow", "arrow3"):
                before, after = (value if kind == "arrow3" else (label, value))
                f_b, f_a = korean_font(28, "medium"), korean_font(32, "bold")
                bw = min(int(f_b.getlength(before)), (right - x) // 2 - 50)
                bl = wrap_text_px(before, f_b, max(80, bw + 2))[:2]
                lh = int(text_height(f_b) * 1.25)
                ty = cy - len(bl) * lh // 2
                for l in bl:
                    draw.text((x, ty), l, font=f_b, fill="#64748B")
                    ty += lh
                ax = x + bw + 22
                draw.text((ax, cy), "→", font=display_font(34, "→"), fill=c.secondary, anchor="lm")
                al = wrap_text_px(after, f_a, max(80, right - ax - 50))[:2]
                lh = int(text_height(f_a) * 1.25)
                ty = cy - len(al) * lh // 2
                for l in al:
                    draw.text((ax + 50, ty), l, font=f_a, fill=c.primary)
                    ty += lh
            else:
                size = 34
                while size > 22:
                    f_v = korean_font(size, "semibold")
                    vl = wrap_text_px(value, f_v, right - x)
                    if len(vl) * int(text_height(f_v) * 1.3) <= row_h - 24:
                        break
                    size -= 2
                vl = vl[:3]
                lh = int(text_height(f_v) * 1.3)
                ty = cy - len(vl) * lh // 2
                for l in vl:
                    draw.text((x, ty), l, font=f_v, fill="#1F2937")
                    ty += lh

    def _layout_gov(self, ctx: "_Ctx", photo: Optional[Image.Image]) -> Image.Image:
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        c, t = ctx.c, ctx.slide.slide_type
        img = Image.new("RGB", (W, H), c.background)
        draw = ImageDraw.Draw(img)
        self._header(img, draw, ctx, dark=False)
        px0, py0, px1, py1 = 46, 140, W - 46, 1205          # 흰 패널
        draw.rounded_rectangle([px0 + 4, py0 + 8, px1 + 4, py1 + 8], radius=40, fill=tint(c.primary, 0.78))
        draw.rounded_rectangle([px0, py0, px1, py1], radius=40, fill="#FFFFFF")
        ix0, ix1 = px0 + 50, px1 - 50
        body = [l.strip() for l in (ctx.slide.body_content or "").splitlines() if l.strip()]
        take = (ctx.slide.takeaway or "").strip()
        band_top = py1 - 130 if take else py1 - 40
        title_color = "#111827"

        if t in ("cover", "single"):
            kicker = ctx.slide.badge or self.TYPE_NAMES.get(t, "")
            checks = body[:3]
            tsz = dict(start=96 if t == "cover" else 76, minimum=52, max_lines=3, x=ix0, width=ix1 - ix0)
            block = (90 if kicker else 0) + self._emph_title(draw, ctx, 0, measure=True, **tsz) + (80 + 78 * len(checks) if checks else 0)
            # 사진이 없으면 표지 내용을 패널 가운데로 (아래가 휑하지 않게)
            y = py0 + 60 if photo is not None else max(py0 + 60, py0 + (band_top - py0 - block) // 2 - 10)
            if kicker:
                f_k = display_font(30, kicker)
                kw = int(f_k.getlength(kicker)) + 60
                draw.rounded_rectangle([(W - kw) // 2, y, (W + kw) // 2, y + 58], radius=29, fill=c.primary)
                draw.text((W // 2, y + 29), kicker, font=f_k, fill="#FFFFFF", anchor="mm")
                y += 90
            y = self._emph_title(draw, ctx, y, color=title_color, emph_color=c.secondary, center=True, **tsz)
            if checks:
                bh = 40 + 78 * len(checks)
                by = y + 40
                draw.rounded_rectangle([ix0, by, ix1, by + bh], radius=26, fill=tint(c.secondary, 0.9))
                self._gov_rows(draw, ctx, checks, (ix0 + 30, by + 20, ix1 - 30, by + bh - 20))
                y = by + bh
            if photo is not None and band_top - y > 220:
                ph_box = (ix0, y + 30, ix1, band_top - 20)
                w_, h_ = ph_box[2] - ph_box[0], ph_box[3] - ph_box[1]
                mask = Image.new("L", (w_, h_), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, w_, h_], radius=26, fill=255)
                img.paste(ImageOps.fit(photo, (w_, h_), method=Image.LANCZOS, centering=(0.5, 0.58)), (ph_box[0], ph_box[1]), mask)  # 넓은 상자: 인물 쪽
            if t == "single" and ((ctx.el.show_phone and ctx.kit.phone) or make_qr(ctx.el) is not None) and band_top - y > 260 and photo is None:
                self._contact_block(img, draw, ctx, (ix0, band_top - 250, ix1, band_top - 20))
        else:
            num = ctx.slide.badge or f"{max(1, ctx.index):02d}"   # 표지 다음 카드부터 01
            sec = self.TYPE_NAMES.get(t, "핵심 내용")
            y = py0 + 50
            f_n = display_font(34, num)
            nw = int(f_n.getlength(num)) + 50
            f_s = display_font(26, sec)
            sw = int(f_s.getlength(sec)) + 70
            draw.rounded_rectangle([ix0 + nw - 30, y + 6, ix0 + nw - 30 + sw, y + 54], radius=24, fill=tint(c.secondary, 0.82))
            draw.text((ix0 + nw + 10, y + 30), sec, font=f_s, fill=c.primary, anchor="lm")
            draw.rounded_rectangle([ix0, y, ix0 + nw, y + 60], radius=30, fill=c.primary)
            draw.text((ix0 + nw / 2, y + 30), num, font=f_n, fill="#FFFFFF", anchor="mm")
            y = self._emph_title(draw, ctx, y + 90, start=64, minimum=40, max_lines=3, x=ix0, width=ix1 - ix0,
                                 color=title_color, emph_color=c.secondary) + 30
            draw.line([ix0, y, ix1, y], fill=c.primary, width=3)
            area = (ix0, y + 10, ix1, band_top - 20)
            if t == "big_number":
                value = ctx.slide.stat_value or ""
                size = 230
                while value and display_font(size, value).getlength(value) > ix1 - ix0 and size > 80:
                    size -= 10
                cy = (area[1] + area[3]) // 2 - 50
                if value:
                    draw.text((W // 2, cy), value, font=display_font(size, value), fill=c.secondary, anchor="mm")
                f_l, ll = self._fit_lines(ctx.slide.stat_label or (body[0] if body else ""), ix1 - ix0, start=38, minimum=26, max_lines=2, weight="semibold")
                ly = cy + size // 2 + 30
                for l in ll:
                    draw.text((W // 2, ly), l, font=f_l, fill="#1F2937", anchor="ma")
                    ly += int(text_height(f_l) * 1.3)
            elif all(normalize_table(ctx.slide.comparison_table)):
                header, rows = normalize_table(ctx.slide.comparison_table)
                self._draw_table(draw, header, rows, (ix0, area[1] + 20, ix1, min(area[3], area[1] + 20 + 120 * (len(rows) + 1))), ctx)
            elif t == "call_to_action":
                has_contact = (ctx.el.show_phone and ctx.kit.phone) or (ctx.el.show_homepage and ctx.kit.homepage) or make_qr(ctx.el) is not None
                split = area[3] - 250 if has_contact else area[3]
                self._gov_rows(draw, ctx, body[:3], (area[0], area[1], area[2], split - 10))
                if has_contact:
                    self._contact_block(img, draw, ctx, (ix0, split, ix1, area[3]))
            else:
                self._gov_rows(draw, ctx, body, area)
        if take:   # 남색 결론 띠
            draw.rounded_rectangle([ix0, band_top, ix1, py1 - 34], radius=20, fill=c.primary)
            f_t, tl = self._fit_lines(take, ix1 - ix0 - 60, start=34, minimum=24, max_lines=1, weight="bold")
            draw.text((W // 2, (band_top + py1 - 34) // 2), tl[0], font=f_t, fill="#FFFFFF", anchor="mm")
        self._footer(draw, ctx, dark=False, y=1290)
        return img

    def _layout_typo_photo(self, ctx: "_Ctx", photo: Image.Image) -> Image.Image:
        """타이포그래피형 + 첨부 사진 배경: 사진을 꽉 채우고, 글씨가 놓이는 띠만 어둡게 덮어 흰 글씨가 읽히게"""
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        c, t = ctx.c, ctx.slide.slide_type
        top = (ctx.slide.text_position or "top") != "bottom"
        cta_phone = f"상담 {ctx.kit.phone}" if (t == "call_to_action" and ctx.el.show_phone and ctx.kit.phone) else ""
        body = [l.strip() for l in (ctx.slide.body_content or "").splitlines() if l.strip()][:2]
        ts = dict(start=84 if t in ("cover", "single") else 66, minimum=46, max_lines=3)
        # 글씨 묶음 높이를 먼저 재서 위치·어둡게 할 범위를 정함
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        th = self._emph_title(probe, ctx, 0, measure=True, **ts)
        ph = self._typo_paragraphs(probe, ctx, body, (M, 0, W - M, 220), start=32, measure=True) if body else 0
        block = 62 + th + (60 + ph if body else 30) + (80 if cta_phone else 0)
        y0 = 170 if top else max(170, 1180 - block)
        band = (0, y0 + block + 40) if top else (y0 - 60, H)
        # 글씨가 위면 하늘 쪽을 더 남기고, 아래면 위쪽(풍경)을 남김
        img = ImageOps.fit(photo.convert("RGB"), (W, H), method=Image.LANCZOS, centering=(0.5, 0.35 if top else 0.45))
        shade = Image.new("L", (1, H))
        for yy in range(H):
            if band[0] <= yy <= band[1]:
                a = 200
            else:
                d = (yy - band[1]) if yy > band[1] else (band[0] - yy)
                a = 200 * max(0.0, 1 - d / 240)
            if top and yy > H - 260:   # 아래 푸터 글씨도 읽히게
                a = max(a, 170 * (yy - (H - 260)) / 260)
            if yy < 140:               # 위 헤더(로고·쪽수)
                a = max(a, 120 * (1 - yy / 140))
            shade.putpixel((0, yy), int(a))
        img = Image.composite(Image.new("RGB", (W, H), "#0B1220"), img, shade.resize((W, H)))
        draw = ImageDraw.Draw(img)
        self._header(img, draw, ctx, dark=True)
        self._tag(draw, ctx, (M, y0), dark=True, height=46)
        y = self._emph_title(draw, ctx, y0 + 62, color="#FFFFFF", emph_color=c.accent, **ts)
        draw.rectangle([M, y + 20, M + 110, y + 31], fill=c.accent)
        y += 60
        if body:
            y += self._typo_paragraphs(draw, ctx, body, (M, y, W - M, y + 220), start=32, color="#F1F5F9", bar=c.accent)
        if cta_phone:
            draw.text((M, y + 20), cta_phone, font=display_font(48, cta_phone), fill="#FFFFFF")
        elif top:
            self._typo_takeaway(draw, ctx, 1160, color="#FFFFFF", arrow=c.accent)
        self._footer(draw, ctx, dark=True)
        return img

    def _layout_typo(self, ctx: "_Ctx") -> Image.Image:
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        c, t = ctx.c, ctx.slide.slide_type
        img = Image.new("RGB", (W, H), c.background)
        draw = ImageDraw.Draw(img)
        self._header(img, draw, ctx, dark=False)
        body = [l.strip() for l in (ctx.slide.body_content or "").splitlines() if l.strip()]
        has_contact = (ctx.el.show_phone and ctx.kit.phone) or (ctx.el.show_homepage and ctx.kit.homepage) or make_qr(ctx.el) is not None

        if t in ("cover", "single"):
            bottom = 860 if (t == "single" and has_contact) else 1080
            ts = dict(start=104 if t == "cover" else 88, minimum=56, max_lines=4)
            paras = body[:3]
            th = self._emph_title(draw, ctx, 0, measure=True, **ts)
            ph = self._typo_paragraphs(draw, ctx, paras, (M, 0, W - M, bottom - 300 - th - 90), start=38 if t == "cover" else 34, measure=True)
            top = 200 + max(0, int((bottom - 200 - (76 + th + 90 + ph)) * 0.45))
            self._tag(draw, ctx, (M, top), height=46)
            y = self._emph_title(draw, ctx, top + 76, **ts)
            draw.rectangle([M, y + 26, M + 120, y + 38], fill=c.secondary)
            self._typo_paragraphs(draw, ctx, paras, (M, y + 90, W - M, bottom), start=38 if t == "cover" else 34)
            if t == "single" and has_contact:
                self._contact_block(img, draw, ctx, (M, 890, W - M, 1110))
            self._typo_takeaway(draw, ctx)
        elif t == "big_number":
            self._tag(draw, ctx, (M, 190), height=46)
            y = self._emph_title(draw, ctx, 270, start=54, minimum=38, max_lines=2)
            value = ctx.slide.stat_value or ""
            size = 300
            while value and display_font(size, value).getlength(value) > W - 2 * M and size > 90:
                size -= 10
            vy = y + 40
            if value:
                fv = display_font(size, value)
                draw.text((M - 6, vy), value, font=fv, fill=c.secondary)
                vy += fv.getbbox(value)[3] + 36   # 글꼴마다 위 여백이 달라 실제 그려진 아래 끝 기준
            label = ctx.slide.stat_label or ""
            if label:
                f_l, l_lines = self._fit_lines(label, W - 2 * M, start=40, minimum=28, max_lines=2, weight="semibold")
                for line in l_lines:
                    draw.text((M, vy), line, font=f_l, fill=c.primary)
                    vy += int(text_height(f_l) * 1.3)
            self._typo_paragraphs(draw, ctx, [b for b in body if b not in (label, value)][:2], (M, vy + 40, W - M, 1100), start=32)
            self._typo_takeaway(draw, ctx)
        elif t == "comparison_table" and all(normalize_table(ctx.slide.comparison_table)):
            self._tag(draw, ctx, (M, 190), height=46)
            y = self._emph_title(draw, ctx, 270, start=64, minimum=42, max_lines=2)
            header, rows = normalize_table(ctx.slide.comparison_table)
            self._draw_table(draw, header, rows, (M, y + 60, W - M, min(1100, y + 60 + 120 * (len(rows) + 1))), ctx)
            self._typo_takeaway(draw, ctx)
        elif t == "event_info":
            self._tag(draw, ctx, (M, 190), height=46)
            y = self._emph_title(draw, ctx, 270, start=68, minimum=44, max_lines=2) + 40
            rows = []
            for line in body[:6]:
                m = re.match(r"^\s*([^:：]{1,8})\s*[:：]\s*(.+)$", line)
                rows.append((m.group(1).strip(), m.group(2).strip()) if m else ("", line))
            if rows:
                row_h = min(150, (1100 - y) // len(rows))
                f_v = korean_font(36, "semibold")
                for i, (label, value) in enumerate(rows):
                    cy = y + i * row_h + row_h // 2
                    draw.line([M, y + i * row_h, W - M, y + i * row_h], fill=tint(c.primary, 0.85), width=2)
                    if label:
                        draw.text((M, cy), label, font=display_font(28, label), fill=c.secondary, anchor="lm")
                    vx = M + (200 if label else 0)
                    vl = wrap_text_px(value, f_v, W - M - vx)[:2]
                    lh = int(text_height(f_v) * 1.3)
                    ty = cy - len(vl) * lh // 2
                    for v in vl:
                        draw.text((vx, ty), v, font=f_v, fill=c.primary)
                        ty += lh
                draw.line([M, y + len(rows) * row_h, W - M, y + len(rows) * row_h], fill=tint(c.primary, 0.85), width=2)
            self._typo_takeaway(draw, ctx)
        elif t == "call_to_action":
            self._tag(draw, ctx, (M, 210), height=46)
            y = self._emph_title(draw, ctx, 300, start=88, minimum=54, max_lines=3)
            draw.rectangle([M, y + 26, M + 120, y + 38], fill=c.secondary)
            self._typo_paragraphs(draw, ctx, body[:3], (M, y + 90, W - M, 860 if has_contact else 1100), start=34)
            if has_contact:
                self._contact_block(img, draw, ctx, (M, 890, W - M, 1110))
            self._typo_takeaway(draw, ctx, 1160)
        else:   # body · numbered 등
            num = ctx.slide.badge or f"{ctx.index + 1:02d}"
            ts = dict(start=76, minimum=46, max_lines=3)
            th = self._emph_title(draw, ctx, 0, measure=True, **ts)
            ph = self._typo_paragraphs(draw, ctx, body, (M, 0, W - M, 1080 - 380 - th - 70), start=40, measure=True)
            top = 160 + max(0, int((1080 - 160 - (200 + th + 70 + ph)) * 0.45))
            draw.text((M - 4, top), num, font=display_font(150, num), fill=tint(c.secondary, 0.25))
            y = self._emph_title(draw, ctx, top + 200, **ts)
            draw.line([M, y + 30, W - M, y + 30], fill=tint(c.primary, 0.85), width=2)
            self._typo_paragraphs(draw, ctx, body, (M, y + 70, W - M, 1080), start=40)
            self._typo_takeaway(draw, ctx)
        self._footer(draw, ctx, dark=False)
        return img

    def _layout_poster_cover(self, ctx: "_Ctx", photo: Optional[Image.Image]) -> Image.Image:
        """포스터형 표지: 꽉 찬 색 배경 + 아주 큰 제목 + 아래쪽 가득 채운 그림"""
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        c = ctx.c
        fill = c.secondary
        ink = self._on_color(fill, c.primary)
        img = Image.new("RGB", (W, H), fill)
        draw = ImageDraw.Draw(img)
        self._header(img, draw, ctx, dark=(ink == "#FFFFFF"))
        f_t, t_lines = self._fit_lines(ctx.slide.lead_headline, W - 2 * M, start=104, minimum=56, max_lines=3, weight="bold")
        lh = int(text_height(f_t) * 1.18)
        y = 190
        for line in t_lines:
            draw.text((W // 2, y), line, font=f_t, fill=ink, anchor="ma")
            y += lh
        sub = next((l.strip() for l in (ctx.slide.body_content or "").splitlines() if l.strip()), "")
        if sub:
            f_s = korean_font(30, "bold")
            sw = min(W - 2 * M, int(f_s.getlength(sub)) + 70)
            y += 24
            draw.rounded_rectangle([(W - sw) // 2, y, (W + sw) // 2, y + 64], radius=32, fill=c.background)
            draw.text((W // 2, y + 32), sub[:34], font=f_s, fill=c.primary, anchor="mm")
            y += 64
        top = max(y + 50, 640)
        box = (0, top, W, H)
        if photo is not None:
            img.paste(self._cover_fit(photo, W, H - top), (0, top))
        else:
            self._illustration(img, ctx, (M, top, W - M, H - M), None)
        return img

    def _layout_color_block(self, ctx: "_Ctx", photo: Optional[Image.Image]) -> Image.Image:
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        d, c = ctx.design, ctx.c
        fill = c.secondary
        ink = self._on_color(fill, c.primary)          # 색 면 위 글자
        soft = tint(fill, 0.55) if ink == "#FFFFFF" else tint(c.primary, 0.35)   # 구분선
        img = Image.new("RGB", (W, H), c.background)
        draw = ImageDraw.Draw(img)
        self._header(img, draw, ctx, dark=False)
        cover = ctx.slide.slide_type == "cover"
        # 제목: 가운데 정렬 큰 글씨
        f_t, t_lines = self._fit_lines(ctx.slide.lead_headline, W - 2 * M, start=64 if cover else 54, minimum=36, max_lines=3 if cover else 2, weight="bold")
        lh = int(text_height(f_t) * 1.2)
        top_y = 170
        for i, line in enumerate(t_lines):
            draw.text((W // 2, top_y + i * lh), line, font=f_t, fill=c.primary, anchor="ma")
        panel_top = top_y + len(t_lines) * lh + 60
        panel_top = max(panel_top, 390)
        draw.rounded_rectangle([0, panel_top, W, H + 80], radius=d.radius, fill=fill)
        body_top, body_bottom = panel_top + 44, 1195
        lines = [l.strip() for l in (ctx.slide.body_content or "").splitlines() if l.strip()]
        if cover:
            # 표지: 색 면 안에 큰 그림(없으면 도형 장식)
            self._illustration(img, ctx, (M, body_top, W - M, min(body_bottom, body_top + 640)), photo)
            sub_y = min(body_bottom, body_top + 640) + 26
            f_s, s_lines = self._fit_lines(" ".join(lines[:2]), W - 2 * M, start=30, minimum=22, max_lines=2, weight="medium")
            for i, line in enumerate(s_lines):
                draw.text((W // 2, sub_y + i * int(text_height(f_s) * 1.3)), line, font=f_s, fill=ink, anchor="ma")
        elif ctx.slide.slide_type == "big_number":
            value = ctx.slide.stat_value or ctx.slide.lead_headline
            size = 230
            while display_font(size, value).getlength(value) > W - 2 * M - 40 and size > 70:
                size -= 10
            cy = (body_top + body_bottom) // 2 - 60
            draw.text((W // 2, cy), value, font=display_font(size, value), fill=ink, anchor="mm")
            f_l, l_lines = self._fit_lines(ctx.slide.stat_label or ctx.slide.body_content, W - 2 * M - 60, start=36, minimum=24, max_lines=3, weight="semibold")
            ly = cy + size // 2 + 40
            for line in l_lines:
                draw.text((W // 2, ly), line, font=f_l, fill=ink, anchor="ma")
                ly += int(text_height(f_l) * 1.3)
        elif ctx.slide.slide_type == "call_to_action":
            has_contact = (ctx.el.show_phone and ctx.kit.phone) or (ctx.el.show_homepage and ctx.kit.homepage) or make_qr(ctx.el) is not None
            card_bottom = 790 if has_contact else body_bottom
            draw.rounded_rectangle([M, body_top, W - M, card_bottom], radius=24, fill="#FFFFFF")
            self._draw_bullets(draw, ctx.slide.body_content or ctx.slide.lead_headline, (M + 36, body_top + 30, W - M - 36, card_bottom - 30), ctx)
            if has_contact:
                self._contact_block(img, draw, ctx, (M, card_bottom + 24, W - M, body_bottom))
        elif normalize_table(ctx.slide.comparison_table)[0] and normalize_table(ctx.slide.comparison_table)[1]:
            header, trows = normalize_table(ctx.slide.comparison_table)
            draw.rounded_rectangle([M, body_top, W - M, body_bottom], radius=24, fill="#FFFFFF")
            self._draw_table(draw, header, trows, (M + 30, body_top + 30, W - M - 30, body_bottom - 30), ctx)
        else:
            rows = []
            for line in lines[:6]:
                m = re.match(r"^\s*([^:：]{1,8})\s*[:：]\s*(.+)$", line)
                rows.append((m.group(1).strip(), m.group(2).strip()) if m else ("", line))
            if rows:
                n = len(rows)
                row_h = min(170, (body_bottom - body_top) // n)
                size = 32
                has_label = any(r[0] for r in rows)
                tx_w = W - 2 * M - (190 if has_label else 60)
                while size > 20:
                    f = korean_font(size, "semibold")
                    lhh = int(text_height(f) * 1.3)
                    if all(len(wrap_text_px(v, f, tx_w)) * lhh + 24 <= row_h for _, v in rows):
                        break
                    size -= 2
                f = korean_font(size, "semibold")
                lhh = int(text_height(f) * 1.3)
                y = body_top + max(0, ((body_bottom - body_top) - row_h * n) // 2)
                for i, (label, value) in enumerate(rows):
                    cy = y + row_h // 2
                    x = M
                    if label:   # 라벨 탭 (밝은 바탕, 진한 글씨)
                        fl = display_font(24, label)
                        lw = int(fl.getlength(label)) + 36
                        draw.rounded_rectangle([x, cy - 26, x + lw, cy + 26], radius=10, fill=c.background)
                        draw.text((x + lw / 2, cy), label, font=fl, fill=c.primary, anchor="mm")
                        x += 150 + 30 if lw < 150 else lw + 30
                    else:
                        draw.ellipse([x, cy - 8, x + 16, cy + 8], fill=ink)
                        x += 40
                    wrapped = wrap_text_px(value, f, W - M - x)[:3]
                    ty = cy - len(wrapped) * lhh // 2
                    for wl in wrapped:
                        draw.text((x, ty), wl, font=f, fill=ink)
                        ty += lhh
                    if i < n - 1:
                        draw.line([M, y + row_h, W - M, y + row_h], fill=soft, width=2)
                    y += row_h
        # 요약 알약 + 푸터
        f_k, k_lines = self._fit_lines(ctx.slide.takeaway, W - 2 * M - 80, start=28, minimum=20, max_lines=1, weight="bold")
        kw = int(f_k.getlength(k_lines[0])) + 80 if k_lines else 0
        if k_lines:
            draw.rounded_rectangle([(W - kw) // 2, 1205, (W + kw) // 2, 1265], radius=30, fill=c.background)
            draw.text((W // 2, 1235), k_lines[0], font=f_k, fill=c.primary, anchor="mm")
        left_parts = [p for p in ((ctx.kit.footer_text or ctx.kit.homepage_short) if ctx.el.show_homepage else "",
                                  ctx.kit.sns_handle if ctx.el.show_sns else "") if p]
        right = f"상담 {ctx.kit.phone}" if ctx.el.show_phone and ctx.kit.phone else ""
        if left_parts:
            draw.text((M, 1305), "  |  ".join(left_parts), font=korean_font(22, "medium"), fill=ink, anchor="lm")
        if right:
            draw.text((W - M, 1305), right, font=display_font(24, right), fill=ink, anchor="rm")
        return img

    def _layout_full_photo(self, ctx: "_Ctx", photo: Image.Image) -> Image.Image:
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        img = self._cover_fit(photo, W, H).convert("RGBA")
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        r, g, b = _rgb(ctx.c.primary)
        for y in range(H):
            a = int(120 * (1 - y / 220)) if y < 220 else int(235 * min(1.0, (y - H * 0.38) / (H * 0.42))) if y > H * 0.38 else 0
            sd.line([(0, y), (W, y)], fill=(r // 3, g // 3, b // 3, a))
        img = Image.alpha_composite(img, shade).convert("RGB")
        draw = ImageDraw.Draw(img)
        self._header(img, draw, ctx, dark=True)

        f_lead, lead_lines = self._fit_lines(ctx.slide.lead_headline, W - 2 * M, start=60, minimum=40, max_lines=3, weight="bold")
        lh = int(text_height(f_lead) * 1.18)
        f_body, body_lines = self._fit_lines((ctx.slide.body_content or "").replace("\n", " "), W - 2 * M, start=30, minimum=24, max_lines=3, weight="medium")
        blh = int(text_height(f_body) * 1.35)
        qr = make_qr(ctx.el) if ctx.slide.slide_type == "call_to_action" else None
        block_h = 66 + lh * len(lead_lines) + 24 + blh * len(body_lines)
        y = 1060 - block_h
        self._tag(draw, ctx, (M, y), dark=True, height=48)
        y += 66
        for line in lead_lines:
            draw.text((M, y), line, font=f_lead, fill="#FFFFFF")
            y += lh
        y += 24
        for line in body_lines:
            draw.text((M, y), line, font=f_body, fill="#E2E8F0")
            y += blh
        if qr is not None:  # 사진 마무리 카드: 오른쪽 위에 QR
            q = qr.resize((190, 190), Image.NEAREST)
            draw.rounded_rectangle([W - M - 214, 160, W - M, 374], radius=16, fill="#FFFFFF")
            img.paste(q, (W - M - 202, 172))
        self._takeaway_bar(draw, ctx, dark=True)
        self._footer(draw, ctx, dark=True)
        return img

    def _layout_big_number(self, draw, ctx: "_Ctx"):
        W, M = self.WIDTH, self.MARGIN
        self._tag(draw, ctx, (M, 175))
        y = self._headline(draw, ctx, 265, start=40, minimum=30, max_lines=2)
        box = (M, max(y + 30, 400), W - M, 1050)
        draw.rounded_rectangle(box, radius=28, fill=tint(ctx.c.primary, 0.9))
        value = ctx.slide.stat_value or ctx.slide.lead_headline
        size = 220
        while display_font(size, value).getlength(value) > (box[2] - box[0] - 80) and size > 70:
            size -= 10
        cy = (box[1] + box[3]) // 2 - 50
        draw.text((W // 2, cy), value, font=display_font(size, value), fill=ctx.c.primary, anchor="mm")
        label = ctx.slide.stat_label or ctx.slide.body_content
        f_label, lines = self._fit_lines(label, box[2] - box[0] - 100, start=34, minimum=24, max_lines=2, weight="medium")
        ly = cy + size // 2 + 40
        for line in lines:
            draw.text((W // 2, ly), line, font=f_label, fill="#334155", anchor="ma")
            ly += int(text_height(f_label) * 1.3)

    def _layout_event_info(self, draw, ctx: "_Ctx"):
        """이벤트·행사 안내: '라벨: 값' 줄을 라벨 칩 + 값 행으로 정리"""
        W, M = self.WIDTH, self.MARGIN
        self._tag(draw, ctx, (M, 175))
        y = self._headline(draw, ctx, 265, start=46, minimum=32, max_lines=2)
        top, bottom = max(y + 30, 380), 1050
        rows = []
        for line in (ctx.slide.body_content or "").splitlines():
            if not line.strip():
                continue
            m = re.match(r"^\s*([^:：]{1,8})\s*[:：]\s*(.+)$", line)
            rows.append((m.group(1).strip(), m.group(2).strip()) if m else ("", line.strip()))
        if not rows:
            return
        size = 32
        while size > 20:
            f_val = korean_font(size, "semibold")
            lh = int(text_height(f_val) * 1.3)
            wrapped = [wrap_text_px(v, f_val, W - 2 * M - 260) or [""] for _, v in rows]
            heights = [max(len(w) * lh, 60) + 36 for w in wrapped]
            if sum(heights) <= bottom - top or size <= 22:
                break
            size -= 2
        total = sum(heights)
        y0 = top + max(0, (bottom - top - total) // 2)
        for (label, _), lines, h in zip(rows, wrapped, heights):
            if y0 + h > bottom + 10:
                break
            draw.rounded_rectangle([M, y0, W - M, y0 + h - 12], radius=18, fill="#FFFFFF", outline="#E2E8F0", width=2)
            if label:
                self._pill(draw, (M + 24, y0 + (h - 12 - 48) // 2), label, display_font(24, label), ctx.c.primary, "#FFFFFF",
                           pad_x=20, height=48, radius=12)
            ty = y0 + (h - 12 - len(lines) * lh) // 2
            for line in lines:
                draw.text((M + 250, ty), line, font=f_val, fill="#0F172A")
                ty += lh
            y0 += h

    def _contact_block(self, img, draw, ctx: "_Ctx", box) -> None:
        """연락처(전화·홈페이지·SNS) + QR — 마무리 카드·1장 카드 공용"""
        x0, y0, x1, y1 = box
        qr = make_qr(ctx.el)
        lines = []
        if ctx.el.show_phone and ctx.kit.phone:
            lines.append(("전화", ctx.kit.phone, 46))
        if ctx.el.show_homepage and ctx.kit.homepage:
            lines.append(("홈페이지", ctx.kit.homepage_short, 32))
        if ctx.el.show_sns and ctx.kit.sns_handle:
            lines.append(("SNS", ctx.kit.sns_handle, 30))
        if not lines and qr is None:
            return
        draw.rounded_rectangle(box, radius=22, fill=tint(ctx.c.secondary, 0.9))
        text_right = x1 - 30
        if qr is not None:
            side = min(y1 - y0 - 40, 230)
            q = qr.resize((side, side), Image.NEAREST)
            img.paste(q, (x1 - side - 24, y0 + (y1 - y0 - side) // 2))
            text_right = x1 - side - 48
            if not lines:
                draw.text((x0 + 30, (y0 + y1) // 2), "QR 코드를 스캔해 주세요", font=display_font(32), fill=ctx.c.primary, anchor="lm")
        total_h = sum(int(s * 1.5) for _, _, s in lines)
        y = (y0 + y1) // 2 - total_h // 2
        for label, value, size in lines:
            draw.text((x0 + 30, y + 4), label, font=korean_font(22, "semibold"), fill="#64748B")
            font = display_font(size, value)
            while font.getlength(value) > text_right - (x0 + 150) and size > 20:
                size -= 2
                font = display_font(size, value)
            draw.text((x0 + 150, y), value, font=font, fill=ctx.c.primary)
            y += int(size * 1.5)

    def _layout_cta(self, img, draw, ctx: "_Ctx"):
        W, M = self.WIDTH, self.MARGIN
        self._tag(draw, ctx, (M, 175))
        y = self._headline(draw, ctx, 265)
        body_bottom = 760
        self._draw_bullets(draw, ctx.slide.body_content, (M + 6, y + 30, W - M, body_bottom), ctx)
        self._contact_block(img, draw, ctx, (M, 790, W - M, 1050))

    def _layout_single(self, ctx: "_Ctx", photo: Optional[Image.Image]) -> Image.Image:
        """1장 카드: 헤드라인·핵심(또는 빅넘버)·요약·연락처/QR을 한 장에"""
        W, H, M = self.WIDTH, self.HEIGHT, self.MARGIN
        img = Image.new("RGB", (W, H), ctx.c.background)
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, W, 20], fill=ctx.c.primary)
        self._header(img, draw, ctx, dark=False)
        y = 170
        if photo is not None:
            pw, ph = W - 2 * M, 330
            pic = self._cover_fit(photo, pw, ph)
            mask = Image.new("L", (pw, ph), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw, ph], radius=24, fill=255)
            img.paste(pic, (M, y), mask)
            y += ph + 30
        y = self._headline(draw, ctx, y, start=52 if photo is None else 44, minimum=32, max_lines=2 if photo is not None else 3)
        has_contact = (ctx.el.show_phone and ctx.kit.phone) or (ctx.el.show_homepage and ctx.kit.homepage) or make_qr(ctx.el) is not None
        content_bottom = 860 if has_contact else 1050
        if ctx.slide.stat_value and photo is None:
            box = (M, y + 20, W - M, y + 230)
            draw.rounded_rectangle(box, radius=22, fill=tint(ctx.c.primary, 0.9))
            v = ctx.slide.stat_value
            size = 120
            while display_font(size, v).getlength(v) > 420 and size > 50:
                size -= 8
            draw.text((M + 40, (box[1] + box[3]) // 2), v, font=display_font(size, v), fill=ctx.c.primary, anchor="lm")
            f_label, lines = self._fit_lines(ctx.slide.stat_label, W - 2 * M - 520, start=30, minimum=22, max_lines=3, weight="medium")
            ly = (box[1] + box[3]) // 2 - int(text_height(f_label) * 1.3 * len(lines)) // 2
            for line in lines:
                draw.text((M + 500, ly), line, font=f_label, fill="#334155")
                ly += int(text_height(f_label) * 1.3)
            y = box[3]
        self._draw_bullets(draw, ctx.slide.body_content, (M + 6, y + 24, W - M, content_bottom), ctx)
        if has_contact:
            self._contact_block(img, draw, ctx, (M, 880, W - M, 1060))
        self._takeaway_bar(draw, ctx, dark=False, top=1085, bottom=1185)
        self._footer(draw, ctx, dark=False)
        return img

    # ------------------------------------------------------------------
    # 그리기 도구
    # ------------------------------------------------------------------
    def _pill(self, draw, xy, text, font, fill, color, pad_x=20, height=50, radius=25, outline=None):
        x, y = xy
        w = int(font.getlength(text)) + pad_x * 2
        draw.rounded_rectangle([x, y, x + w, y + height], radius=radius, fill=fill, outline=outline, width=2 if outline else 0)
        draw.text((x + w / 2, y + height / 2), text, font=font, fill=color, anchor="mm")
        return w

    def _fit_lines(self, text: str, max_width: int, start: int, minimum: int, max_lines: int, weight: str = "regular"):
        """지정 줄 수 안에 들어갈 때까지 폰트 크기를 줄여가며 줄바꿈 (굵은 문구는 디스플레이 폰트)"""
        size = start
        while True:
            font = display_font(size, text) if weight == "bold" else korean_font(size, weight)
            lines = wrap_text_px(text or "", font, max_width)
            if len(lines) <= max_lines or size <= minimum:
                if len(lines) > max_lines:
                    lines = lines[:max_lines]
                    lines[-1] = lines[-1].rstrip() + "…"
                return font, lines
            size -= 2

    def _bullets_height(self, body: str, width: int) -> int:
        """가장 큰 글자(30)로 그렸을 때 필요한 높이"""
        paragraphs = [p.strip() for p in (body or "").split("\n") if p.strip()]
        font = korean_font(30, "medium")
        lh = int(text_height(font) * 1.35)
        lines = sum(len(wrap_text_px(p, font, width - 34)) for p in paragraphs)
        return lines * lh + max(0, len(paragraphs) - 1) * int(lh * 0.45)

    def _draw_bullets(self, draw, body: str, box, ctx: "_Ctx"):
        x0, y0, x1, y1 = box
        paragraphs = [p.strip() for p in (body or "").split("\n") if p.strip()]
        if not paragraphs or y1 - y0 < 40:
            return
        size = 30
        while size >= 20:
            font = korean_font(size, "medium")
            lh = int(text_height(font) * 1.35)
            wrapped = [wrap_text_px(p, font, x1 - x0 - 34) for p in paragraphs]
            needed = sum(len(w) * lh for w in wrapped) + max(0, len(wrapped) - 1) * int(lh * 0.45)
            if needed <= (y1 - y0) or size == 20:
                break
            size -= 2
        y = y0 + max(0, ((y1 - y0) - needed) // 2)
        for lines in wrapped:
            if y + lh > y1:
                break
            draw.ellipse([x0, y + lh // 2 - 6, x0 + 12, y + lh // 2 + 6], fill=ctx.c.secondary)
            for line in lines:
                if y + lh > y1:
                    break
                draw.text((x0 + 34, y), line, font=font, fill="#334155")
                y += lh
            y += int(lh * 0.45)

    def _draw_table(self, draw, header: List[str], rows: List[List[str]], box, ctx: "_Ctx"):
        """열 폭 자동 배분 + 셀 내부 줄바꿈 + 넘침 시 글자 크기 축소"""
        x0, y0, x1, y1 = box
        total_w = x1 - x0
        n_cols = len(header)
        first_w = int(total_w * (0.30 if n_cols > 2 else 0.34))
        other_w = (total_w - first_w) // max(1, n_cols - 1)
        col_ws = [first_w] + [other_w] * (n_cols - 1)
        pad = 16
        size = 26
        while True:
            font = korean_font(size, "medium")
            bold = korean_font(size, "bold")
            lh = int(text_height(font) * 1.25)
            cell = lambda text, w, f: wrap_text_px(text, f, w - pad * 2) or [""]
            head_lines = [cell(h, w, bold) for h, w in zip(header, col_ws)]
            head_h = max(len(l) for l in head_lines) * lh + pad * 2
            row_lines = [[cell(c, w, bold if i == 0 else font) for i, (c, w) in enumerate(zip(r, col_ws))] for r in rows]
            row_hs = [max(len(l) for l in rl) * lh + pad * 2 for rl in row_lines]
            if head_h + sum(row_hs) <= (y1 - y0) or size <= 18:
                break
            size -= 2
        y = y0 + max(0, ((y1 - y0) - head_h - sum(row_hs)) // 2)
        draw.rounded_rectangle([x0, y, x1, y + head_h], radius=10, fill=ctx.c.primary)
        x = x0
        for lines, w in zip(head_lines, col_ws):
            ty = y + pad
            for line in lines:
                draw.text((x + pad, ty), line, font=bold, fill="#FFFFFF")
                ty += lh
            x += w
        y += head_h
        for r_idx, (lines_per_cell, rh) in enumerate(zip(row_lines, row_hs)):
            if y + rh > y1 + 4:
                break
            draw.rectangle([x0, y, x1, y + rh], fill=ctx.c.background if r_idx % 2 == 0 else "#FFFFFF")
            draw.line([x0, y + rh, x1, y + rh], fill="#E2E8F0", width=1)
            x = x0
            for c_idx, (lines, w) in enumerate(zip(lines_per_cell, col_ws)):
                color = ctx.c.secondary if (c_idx == n_cols - 1 and n_cols > 2) else "#0F172A" if c_idx == 0 else \
                    ("#334155" if n_cols > 2 else ctx.c.primary)
                ty = y + pad
                for line in lines:
                    draw.text((x + pad, ty), line, font=bold if c_idx == 0 else font, fill=color)
                    ty += lh
                x += w
            y += rh

    # ------------------------------------------------------------------
    # HTML 번들
    # ------------------------------------------------------------------
    def _render_html_bundle(self, storyboard: EditableStoryboard):
        templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
        if not os.path.exists(os.path.join(templates_dir, "card_template.html")):
            return
        env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape(["html"]))
        template = env.get_template("card_template.html")
        kit = self._kit(storyboard)
        el = storyboard.brand_elements
        total = len(storyboard.card_slides)
        for i, slide in enumerate(storyboard.card_slides):
            table_headers, table_rows = normalize_table(slide.comparison_table)
            asset = storyboard.asset(slide.asset_id)
            photo_src = ""
            if asset and asset.preview_path and slide.slide_type not in ("comparison_table", "big_number", "event_info"):
                photo_src = os.path.relpath(asset.preview_path, self.export_dir).replace(os.sep, "/")
            html_content = template.render(
                photo_src=photo_src,
                bg_gradient=f"linear-gradient(180deg, {kit.colors.background} 0%, {tint(kit.colors.secondary, 0.92)} 100%)",
                slide_index=i + 1, total_slides=total,
                slide_type_name=self.TYPE_NAMES.get(slide.slide_type, slide.slide_type.upper()),
                tag_bg=tint(kit.colors.secondary, 0.85), tag_color=kit.colors.primary, primary=kit.colors.primary,
                secondary=kit.colors.secondary, accent=kit.colors.accent,
                headline_size=38, lead_headline=emphasis.strip(slide.lead_headline), body_content=slide.body_content, takeaway=slide.takeaway,
                stat_value=slide.stat_value, stat_label=slide.stat_label,
                comparison_table=bool(table_rows), table_headers=table_headers, table_rows=table_rows,
                brand_name=kit.brand_name if el.logo != "none" else "", tagline=kit.tagline,
                footer_left=(kit.footer_text or kit.homepage_short) if el.show_homepage else "",
                footer_phone=kit.phone if el.show_phone else "",
            )
            with open(os.path.join(self.export_dir, f"slide_{i+1:02d}.html"), "w", encoding="utf-8") as hf:
                hf.write(html_content)


class _Ctx:
    """렌더링 한 장에 필요한 값 묶음"""
    def __init__(self, kit: bk.BrandKit, elements: BrandElements, index: int, total: int, slide: CardSlide,
                 design: Optional[design_templates.DesignTemplate] = None):
        self.kit, self.el, self.index, self.total, self.slide = kit, elements, index, total, slide
        self.design = design or design_templates.get(None)
        self.c = kit.colors.model_copy(update=self.design.colors) if self.design.colors else kit.colors
        self.emph: List[str] = []   # 제목에서 형광펜으로 칠할 강조어
