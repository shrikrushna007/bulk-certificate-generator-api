from __future__ import annotations

import os
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph


class CertificateRenderer:
    """Renders the application's single predefined certificate design."""

    def render(
        self,
        *,
        destination: Path,
        recipient_name: str,
        certificate_title: str,
        issuing_organization: str,
        issue_date: str,
        certificate_id: str,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = destination.with_suffix(".tmp")
        try:
            self._draw(
                temporary_path,
                recipient_name=recipient_name,
                certificate_title=certificate_title,
                issuing_organization=issuing_organization,
                issue_date=issue_date,
                certificate_id=certificate_id,
            )
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _draw(
        self,
        destination: Path,
        *,
        recipient_name: str,
        certificate_title: str,
        issuing_organization: str,
        issue_date: str,
        certificate_id: str,
    ) -> None:
        page_width, page_height = landscape(A4)
        canvas = Canvas(str(destination), pagesize=(page_width, page_height))
        canvas.setTitle(f"Certificate - {recipient_name}")
        canvas.setAuthor(issuing_organization)

        navy = colors.HexColor("#17324D")
        gold = colors.HexColor("#C59A3D")
        muted = colors.HexColor("#596775")

        canvas.setFillColor(colors.HexColor("#FAFBFC"))
        canvas.rect(0, 0, page_width, page_height, fill=1, stroke=0)
        canvas.setStrokeColor(navy)
        canvas.setLineWidth(3)
        canvas.rect(12 * mm, 12 * mm, page_width - 24 * mm, page_height - 24 * mm)
        canvas.setStrokeColor(gold)
        canvas.setLineWidth(1)
        canvas.rect(17 * mm, 17 * mm, page_width - 34 * mm, page_height - 34 * mm)

        canvas.setFillColor(navy)
        canvas.setFont("Helvetica-Bold", 29)
        canvas.drawCentredString(page_width / 2, page_height - 48 * mm, "CERTIFICATE")
        canvas.setFillColor(gold)
        canvas.setFont("Helvetica-Bold", 14)
        canvas.drawCentredString(page_width / 2, page_height - 59 * mm, "OF ACHIEVEMENT")

        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 12)
        canvas.drawCentredString(
            page_width / 2,
            page_height - 76 * mm,
            "This certificate is presented to",
        )

        self._draw_fitted_name(canvas, recipient_name, page_width / 2, page_height - 96 * mm)
        canvas.setStrokeColor(gold)
        canvas.setLineWidth(0.8)
        canvas.line(55 * mm, page_height - 112 * mm, page_width - 55 * mm, page_height - 112 * mm)

        self._draw_wrapped_centered(
            canvas,
            f"for successfully completing <b>{escape(certificate_title)}</b>",
            y=page_height - 126 * mm,
            width=190 * mm,
            font_size=13,
            color=navy,
        )
        self._draw_wrapped_centered(
            canvas,
            f"Issued by <b>{escape(issuing_organization)}</b>",
            y=page_height - 148 * mm,
            width=180 * mm,
            font_size=11,
            color=muted,
        )

        canvas.setFillColor(navy)
        canvas.setFont("Helvetica", 9)
        canvas.drawString(27 * mm, 28 * mm, f"Issue date: {issue_date}")
        canvas.drawRightString(page_width - 27 * mm, 28 * mm, f"Certificate ID: {certificate_id}")
        canvas.save()

    @staticmethod
    def _draw_fitted_name(canvas: Canvas, name: str, x: float, y: float) -> None:
        max_width = 205 * mm
        font_size = 28.0
        while font_size > 18 and stringWidth(name, "Helvetica-Bold", font_size) > max_width:
            font_size -= 1
        if stringWidth(name, "Helvetica-Bold", font_size) <= max_width:
            canvas.setFillColor(colors.HexColor("#17324D"))
            canvas.setFont("Helvetica-Bold", font_size)
            canvas.drawCentredString(x, y, name)
            return

        style = ParagraphStyle(
            name="RecipientName",
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=20,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#17324D"),
            splitLongWords=True,
        )
        paragraph = Paragraph(escape(name), style)
        _, height = paragraph.wrap(max_width, 30 * mm)
        paragraph.drawOn(canvas, x - max_width / 2, y - height / 2)

    @staticmethod
    def _draw_wrapped_centered(
        canvas: Canvas,
        text: str,
        *,
        y: float,
        width: float,
        font_size: float,
        color: colors.Color,
    ) -> None:
        style = ParagraphStyle(
            name="CertificateText",
            fontName="Helvetica",
            fontSize=font_size,
            leading=font_size * 1.35,
            alignment=TA_CENTER,
            textColor=color,
        )
        paragraph = Paragraph(text, style)
        _, height = paragraph.wrap(width, 30 * mm)
        paragraph.drawOn(canvas, (landscape(A4)[0] - width) / 2, y - height)
