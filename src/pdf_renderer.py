from pathlib import Path

from .utils import find_unicode_font


def render_pdf(title: str, content: str, output: Path) -> None:
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RuntimeError("fpdf2 is unavailable. Install requirements.txt.") from exc

    class ArticlePDF(FPDF):
        def footer(self) -> None:
            self.set_y(-15)
            self.set_font("ArticleFont", size=9)
            self.set_text_color(119, 119, 119)
            self.cell(0, 10, f"Page {self.page_no()}", align="C")

    pdf = ArticlePDF()
    font_path = find_unicode_font()
    for style in ("", "B", "I", "BI"):
        pdf.add_font("ArticleFont", style=style, fname=str(font_path))
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_title(title)
    pdf.set_font("ArticleFont", "B", 22)
    pdf.set_text_color(24, 33, 43)
    pdf.multi_cell(0, 12, title)
    pdf.set_draw_color(59, 110, 165)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)
    pdf.set_font("ArticleFont", size=10)
    pdf.set_text_color(32, 33, 36)
    pdf.write_html(content, font_family="ArticleFont")
    pdf.output(output)
