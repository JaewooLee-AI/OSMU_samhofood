"""
Stitch Design System Token & Styles for OSMU Media Studio
Based on Civic Trust Desktop & Cognitive Clarity Design Systems
"""
import flet as ft

class StitchColors:
    # Primary & Secondary Branding (Civic Trust & Modern Tech)
    PRIMARY = "#00236F"            # Institutional Deep Navy
    PRIMARY_CONTAINER = "#1E3A8A"  # Civic Navy Accent
    PRIMARY_LIGHT = "#3B82F6"      # Soft Sky Blue
    ON_PRIMARY = "#FFFFFF"

    SECONDARY = "#2563EB"          # Action Blue
    SECONDARY_CONTAINER = "#DBEAFE"# Light Ice Blue
    ON_SECONDARY = "#FFFFFF"

    # Backgrounds & Surfaces
    CANVAS = "#F8FAFC"             # Slate Canvas (Eye-friendly office base)
    SURFACE_WHITE = "#FFFFFF"      # Crisp White Card Surface
    SURFACE_VARIANT = "#F1F5F9"    # Light Slate Surface
    SIDEBAR_BG = "#0F172A"         # Midnight Slate (High-contrast navigation shell)
    SIDEBAR_SURFACE = "#1E293B"    # Midnight Slate Inner Card

    # Borders & Dividers
    BORDER_DEFAULT = "#E2E8F0"     # Standard structural container border
    BORDER_FOCUS = "#2563EB"       # Focused boundary
    BORDER_MUTED = "#CBD5E1"       # Subtle divider

    # Text & Typography Hierarchy
    TEXT_PRIMARY = "#0F172A"       # High emphasis black-navy
    TEXT_SECONDARY = "#475569"     # Medium emphasis body
    TEXT_MUTED = "#64748B"         # Low emphasis / hints
    TEXT_LIGHT = "#94A3B8"         # Light metadata
    TEXT_ON_DARK = "#F8FAFC"       # White text on dark shell
    TEXT_ON_DARK_MUTED = "#94A3B8" # Muted text on dark shell

    # Status & Semantics
    SUCCESS = "#10B981"            # Logic Green
    SUCCESS_BG = "#ECFDF5"
    SUCCESS_BORDER = "#A7F3D0"

    WARNING = "#F59E0B"            # Insight Amber
    WARNING_BG = "#FFFBEB"
    WARNING_BORDER = "#FDE68A"

    ERROR = "#EF4444"              # Alert Red
    ERROR_BG = "#FEF2F2"
    ERROR_BORDER = "#FECACA"

    ACCENT_AMBER = "#FFB800"       # High visibility highlight
    ACCENT_PURPLE = "#8B5CF6"      # AI Synthesis Badge

class StitchFonts:
    FAMILY = "Noto Sans, Pretendard, sans-serif"

def create_badge(text: str, bg_color: str, text_color: str, border_color: str = None, icon: str = None):
    """Stitch 스타일의 세련된 Pill 뱃지 생성"""
    controls = []
    if icon:
        controls.append(ft.Icon(name=icon, size=12, color=text_color))
    controls.append(
        ft.Text(
            text,
            size=11,
            weight=ft.FontWeight.W_600,
            color=text_color,
        )
    )
    return ft.Container(
        content=ft.Row(
            controls=controls,
            spacing=4,
            alignment=ft.MainAxisAlignment.CENTER,
            tight=True,
        ),
        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
        border_radius=ft.BorderRadius.all(9999),
        bgcolor=bg_color,
        border=ft.Border.all(1, border_color) if border_color else None,
    )

def create_card_container(content: ft.Control, padding: int = 16, title: str = None, subtitle: str = None, action: ft.Control = None):
    """Stitch 스타일의 고품격 카드 래퍼"""
    header_elements = []
    if title:
        header_row_controls = [
            ft.Text(title, size=15, weight=ft.FontWeight.W_700, color=StitchColors.TEXT_PRIMARY)
        ]
        if subtitle:
            header_row_controls.append(
                ft.Text(subtitle, size=12, color=StitchColors.TEXT_MUTED)
            )
        header_elements.append(
            ft.Row(
                controls=[
                    ft.Column(controls=header_row_controls, spacing=2),
                    action if action else ft.Container()
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
        )
        header_elements.append(ft.Divider(height=1, color=StitchColors.BORDER_DEFAULT))

    return ft.Container(
        content=ft.Column(
            controls=header_elements + [content],
            spacing=12,
        ),
        bgcolor=StitchColors.SURFACE_WHITE,
        border=ft.Border.all(1, StitchColors.BORDER_DEFAULT),
        border_radius=ft.BorderRadius.all(10),
        padding=ft.Padding.all(padding),
        shadow=ft.BoxShadow(
            spread_radius=0,
            blur_radius=4,
            color=ft.Colors.with_opacity(0.04, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        ),
    )
