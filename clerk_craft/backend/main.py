"""Основной файл FastAPI сервера для ClickJurist."""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import mm

from clerk_craft.backend.services.llm_chain import (
    run_llm_pipeline,
    generate_checklist,
    generate_document,
)


# Регистрация кириллического шрифта для PDF
def register_cyrillic_font():
    """Регистрация шрифта FreeSans для поддержки кириллицы."""
    try:
        pdfmetrics.registerFont(TTFont('FreeSans', 'FreeSans'))
    except Exception:
        try:
            pdfmetrics.registerFont(TTFont('FreeSans', 'DejaVuSans'))
        except Exception:
            pass


register_cyrillic_font()

app = FastAPI(
    title="ClickJurist API",
    description="Юридический помощник на базе ИИ"
)

# Настройка CORS для фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Монтируем статические файлы
app.mount("/static", StaticFiles(directory="clerk_craft/frontend"), name="static")


class QueryRequest(BaseModel):
    """Модель запроса от пользователя (универсальная)."""
    query: str


class GenerateResponse(BaseModel):
    """Модель ответа для /api/generate — только финал LLM-2 (без черновика LLM-1)."""
    response: str | None
    error: str | None = None
    warning: str | None = None  # не фатальное предупреждение (например, деградация до LLM-1)


class ChecklistRequest(BaseModel):
    """Запрос чек-листа: передаём вопрос и финальный ответ, чтобы не делать лишний вызов LLM."""
    query: str
    final_answer: str


class ChecklistResponse(BaseModel):
    """Ответ чек-листа."""
    checklist: str | None
    error: str | None = None


class DocumentRequest(BaseModel):
    """Запрос шаблона документа."""
    query: str
    doc_type: str  # 'isk' | 'pretension' | 'zhaloba'


class DocumentResponse(BaseModel):
    """Ответ с шаблоном документа."""
    document: str | None
    doc_type: str | None
    error: str | None = None


@app.get("/")
async def read_index():
    """Отдача главной страницы."""
    return FileResponse("clerk_craft/frontend/index.html")


@app.post("/api/generate", response_model=GenerateResponse)
async def generate_legal_response(request: QueryRequest):
    """
    Эндпоинт для генерации юридического ответа.
    Возвращает ТОЛЬКО финальный ответ LLM-2. Черновик LLM-1 клиенту не показывается.
    """
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Запрос не может быть пустым")

    result = run_llm_pipeline(request.query.strip())

    if "error" in result:
        return GenerateResponse(response=None, error=result["error"])

    return GenerateResponse(
        response=result.get("final_verified"),
        error=None,
        warning=result.get("warning"),  # если LLM-2 упала, сюда придёт пояснение
    )


@app.post("/api/checklist", response_model=ChecklistResponse)
async def checklist_endpoint(request: ChecklistRequest):
    """
    Генерация чек-листа по финальному ответу.
    Вызывается ТОЛЬКО по кнопке «Чек-лист» — не тратит токены заранее.
    """
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Запрос не может быть пустым")
    if not request.final_answer or not request.final_answer.strip():
        raise HTTPException(status_code=400, detail="Нужен final_answer из /api/generate")

    try:
        checklist = generate_checklist(request.query.strip(), request.final_answer)
    except Exception as e:
        return ChecklistResponse(checklist=None, error=f"Сбой генерации чек-листа: {e}")
    if not checklist or not isinstance(checklist, str):
        return ChecklistResponse(checklist=None, error="Чек-лист не сгенерирован (пустой ответ)")
    if checklist.startswith("Ошибка"):
        return ChecklistResponse(checklist=None, error=checklist)
    return ChecklistResponse(checklist=checklist, error=None)


@app.post("/api/document", response_model=DocumentResponse)
async def document_endpoint(request: DocumentRequest):
    """
    Генерация шаблона документа (иск / претензия / жалоба).
    Вызывается ТОЛЬКО по кнопке «Шаблон документа».
    """
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Запрос не может быть пустым")
    if not request.doc_type or not request.doc_type.strip():
        raise HTTPException(status_code=400, detail="Укажите doc_type: 'isk' | 'pretension' | 'zhaloba'")

    try:
        document = generate_document(request.query.strip(), request.doc_type.strip())
    except Exception as e:
        return DocumentResponse(document=None, doc_type=request.doc_type, error=f"Сбой генерации документа: {e}")
    if not document or not isinstance(document, str):
        return DocumentResponse(document=None, doc_type=request.doc_type, error="Документ не сгенерирован (пустой ответ)")
    if document.startswith("Ошибка"):
        return DocumentResponse(document=None, doc_type=request.doc_type, error=document)
    return DocumentResponse(document=document, doc_type=request.doc_type, error=None)


@app.post("/api/generate-pdf")
async def generate_pdf(request: QueryRequest):
    """
    Эндпоинт для генерации PDF-документа с юридической консультацией.
    Использует тот же пайплайн, что /api/generate (внутри — LLM-1 → LLM-2).
    """
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Запрос не может быть пустым")

    result = run_llm_pipeline(request.query.strip())

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    if not result.get("final_verified"):
        raise HTTPException(status_code=500, detail="Не удалось сгенерировать ответ")

    pdf_buffer = io.BytesIO()

    try:
        doc = SimpleDocTemplate(
            pdf_buffer,
            pagesize=A4,
            leftMargin=30*mm,
            rightMargin=20*mm,
            topMargin=20*mm,
            bottomMargin=20*mm
        )

        styles = getSampleStyleSheet()

        style_normal = styles['Normal']
        style_normal.fontName = 'FreeSans'
        style_normal.fontSize = 11
        style_normal.leading = 15

        style_heading = styles['Heading1']
        style_heading.fontName = 'FreeSans'
        style_heading.fontSize = 16
        style_heading.spaceAfter = 12

        story = []

        story.append(Paragraph("Юридическая консультация", style_heading))
        story.append(Spacer(1, 10))

        story.append(Paragraph(f"<b>Вопрос:</b> {request.query}", style_normal))
        story.append(Spacer(1, 15))

        story.append(Paragraph("<b>Ответ:</b>", style_normal))
        story.append(Spacer(1, 8))

        response_text = result["final_verified"]
        paragraphs = response_text.split('\n\n')

        for para in paragraphs:
            if para.strip():
                story.append(Paragraph(para.replace('\n', '<br/>'), style_normal))
                story.append(Spacer(1, 8))

        story.append(Spacer(1, 30))
        story.append(Paragraph("_________________________", style_normal))
        story.append(Paragraph("(подпись)", style_normal))

        doc.build(story)

    except Exception as e:
        pdf_buffer = io.BytesIO()
        c = canvas.Canvas(pdf_buffer, pagesize=A4)

        try:
            c.setFont("FreeSans", 11)
        except Exception:
            c.setFont("Helvetica", 11)

        c.drawString(30*mm, 260*mm, "Юридическая консультация")
        c.drawString(30*mm, 245*mm, f"Вопрос: {request.query[:100]}...")
        c.drawString(30*mm, 230*mm, "Ответ:")

        text_obj = c.beginText(30*mm, 220*mm)
        text_obj.textLines(result["final_verified"][:2000])
        c.drawText(text_obj)

        c.showPage()
        c.save()

    pdf_buffer.seek(0)

    return FileResponse(
        pdf_buffer,
        media_type="application/pdf",
        filename="jurist_konsultaciya.pdf",
        background=None
    )


@app.get("/api/health")
async def health_check():
    """Проверка состояния сервера."""
    return {"status": "ok", "message": "Сервер ClickJurist работает"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
