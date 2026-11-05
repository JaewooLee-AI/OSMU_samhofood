"""검토 시안 — 상사 보고·결재용으로 카드 전체를 한 장(A4)에 모은 이미지와 PDF.

PDF 1쪽: 작업 정보 + 카드 모아보기 + 점검 결과 / 2쪽부터: 카드 한 장씩 크게.
"""
import os
from typing import List, Tuple
from PIL import Image, ImageDraw
from services.fonts import korean_font, display_font, wrap_text_px
from services.card_formats import get_format

A4 = (2480, 3508)  # 300dpi 세로
MARGIN = 140


def build(project, kit, card_paths: List[str], warnings: List[str], out_dir: str) -> Tuple[str, str]:
    """(시안 PNG 경로, PDF 경로) 반환"""
    os.makedirs(out_dir, exist_ok=True)
    W, H = A4
    page = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(page)
    sb = project.storyboard

    # 머리말
    d.rectangle([0, 0, W, 28], fill=kit.colors.primary)
    d.text((MARGIN, 120), "카드뉴스 검토 시안", font=display_font(64), fill=kit.colors.primary)
    title = sb.title if sb else project.title
    y = 230
    for line in wrap_text_px(title, display_font(52, title), W - 2 * MARGIN)[:2]:
        d.text((MARGIN, y), line, font=display_font(52, line), fill="#0F172A")
        y += 72
    info = [f"브랜드: {kit.brand_name}", f"형식: {get_format(sb.card_format)['name'] if sb else '-'} · {len(card_paths)}장",
            f"작성: {project.created_at}  ·  수정: {project.updated_at}", f"상태: {project.status_label}"]
    if project.confirmation:
        c = project.confirmation
        info.append(f"확정: {c.by} · {c.at}" + (f" · 메모: {c.note}" if c.note else ""))
    f_info = korean_font(34, "medium")
    y += 20
    for line in info:
        d.text((MARGIN, y), line, font=f_info, fill="#475569")
        y += 52
    y += 30
    d.line([MARGIN, y, W - MARGIN, y], fill="#E2E8F0", width=3)
    y += 50

    # 카드 모아보기 (최대 4열)
    n = max(1, len(card_paths))
    cols = 4 if n > 6 else 3 if n > 2 else n
    gap = 40
    tw = (W - 2 * MARGIN - gap * (cols - 1)) // cols
    th = int(tw * 1350 / 1080)
    rows = (n + cols - 1) // cols
    reserve = 520  # 점검 결과 영역
    if y + rows * (th + 70) > H - reserve:  # 넘치면 축소
        th = (H - reserve - y) // rows - 70
        tw = int(th * 1080 / 1350)
    f_no = korean_font(28, "semibold")
    for i, path in enumerate(card_paths):
        r, c = divmod(i, cols)
        x0 = MARGIN + c * (tw + gap)
        y0 = y + r * (th + 70)
        with Image.open(path) as im:
            page.paste(im.convert("RGB").resize((tw, th), Image.LANCZOS), (x0, y0))
        d.rectangle([x0, y0, x0 + tw, y0 + th], outline="#CBD5E1", width=2)
        d.text((x0, y0 + th + 14), f"{i + 1}장", font=f_no, fill="#64748B")
    y += rows * (th + 70) + 20

    # 표현 점검 결과
    d.text((MARGIN, y), "표현 점검 결과", font=display_font(40), fill=kit.colors.primary)
    y += 70
    f_w = korean_font(28, "regular")
    lines = warnings or ["✓ 자동 점검에서 확인이 필요한 표현이 발견되지 않았습니다."]
    for wline in lines[:8]:
        for part in wrap_text_px(wline.replace("⚠️", "!").strip(), f_w, W - 2 * MARGIN)[:2]:
            if y > H - 160:
                break
            d.text((MARGIN, y), part, font=f_w, fill="#B45309" if warnings else "#047857")
            y += 42
    if len(lines) > 8:
        d.text((MARGIN, y), f"…외 {len(lines) - 8}건", font=f_w, fill="#B45309")
    d.text((MARGIN, H - 110), "※ 자동 점검은 검토 보조 기능입니다. 게시 전 최종 확인은 담당 부서에서 해 주세요.",
           font=korean_font(26, "regular"), fill="#94A3B8")

    png_path = os.path.join(out_dir, "검토시안.png")
    page.save(png_path, "PNG")

    # PDF: 1쪽 시안 + 카드별 1쪽
    pages = []
    for path in card_paths:
        with Image.open(path) as im:
            card = im.convert("RGB")
        p = Image.new("RGB", A4, "white")
        cw = W - 2 * MARGIN
        ch = int(cw * card.height / card.width)
        p.paste(card.resize((cw, ch), Image.LANCZOS), (MARGIN, (H - ch) // 2))
        pages.append(p)
    pdf_path = os.path.join(out_dir, "검토시안.pdf")
    page.save(pdf_path, "PDF", resolution=300, save_all=True, append_images=pages)
    return png_path, pdf_path
