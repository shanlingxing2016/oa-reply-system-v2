from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, BackgroundTasks
from sqlalchemy.orm import Session
from pathlib import Path
import shutil
import uuid
import re
import traceback
from database import get_db
from models import Case, Document, Comparison
from services.pdf_parser import PDFParser
from config import UPLOAD_DIR, MAX_FILE_SIZE

router = APIRouter(prefix="/api", tags=["documents"])

pdf_parser = PDFParser()


def _parse_in_background(doc_id: int, stored_path: str, doc_type: str, case_id: int):
    """后台解析文档并更新数据库"""
    from database import SessionLocal
    db_bg = SessionLocal()
    try:
        result = pdf_parser.parse(stored_path)
        text = result.get("full_text", "")
        if text:
            doc_bg = db_bg.query(Document).filter(Document.id == doc_id).first()
            if doc_bg:
                doc_bg.extracted_text = text
                db_bg.commit()
                # 自动提取申请号/发明名称
                if doc_type in ("oa", "patent"):
                    meta = _extract_case_meta(text)
                    if meta:
                        case_bg = db_bg.query(Case).filter(Case.id == case_id).first()
                        if case_bg:
                            is_placeholder = (not case_bg.case_number) or case_bg.case_number.startswith("待识别-")
                            changed = False
                            if meta.get("case_number") and is_placeholder:
                                case_bg.case_number = meta["case_number"]
                                changed = True
                            if meta.get("case_name") and (not case_bg.case_name or case_bg.case_name.strip() == ""):
                                case_bg.case_name = meta["case_name"]
                                changed = True
                            if changed:
                                from datetime import datetime
                                case_bg.updated_at = datetime.now()
                                db_bg.commit()
    except Exception:
        traceback.print_exc()
    finally:
        db_bg.close()

ALLOWED_TYPES = {"oa": "审查意见通知书", "patent": "申请文件", "d1": "对比文件D1", "d2": "对比文件2", "d3": "对比文件3", "d4": "对比文件4", "d5": "对比文件5", "template": "参考意见陈述书"}


def _extract_case_meta(text: str) -> dict:
    """从文本中提取申请号和发明名称"""
    result = {}
    # 申请号：多种格式
    patterns_num = [
        r'申请号[：:\s]*([0-9]{13}[A-Z]?)',           # 标准13位
        r'申请号[：:\s]*([0-9A-Z\-\.]{10,20})',       # 通用格式
        r'Application No\.?\s*[：:\s]*([0-9A-Z\-\.]{10,20})',
    ]
    for p in patterns_num:
        m = re.search(p, text[:3000])
        if m:
            result['case_number'] = m.group(1).strip()
            break

    # 发明名称
    patterns_name = [
        r'发明名称[：:\s]*[\n]?\s*(.{5,80})',
        r'名\s*称[：:\s]*[\n]?\s*(.{5,80})',
        r'Title[：:\s]*[\n]?\s*(.{5,80})',
    ]
    for p in patterns_name:
        m = re.search(p, text[:3000])
        if m:
            name = m.group(1).strip()
            # 去掉换行及多余空格
            name = re.sub(r'\s+', ' ', name).strip()
            # 截取到明显结束符
            name = re.split(r'[（(申请人发明人\n]', name)[0].strip()
            if 4 < len(name) < 100:
                result['case_name'] = name
                break
    return result


@router.post("/cases/{case_id}/documents")
def upload_document(
    case_id: int,
    doc_type: str = Query(..., description="oa/patent/d1/d2"),
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    if doc_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"doc_type 须为: {', '.join(ALLOWED_TYPES.keys())}")

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 检查是否已存在同类型文档，存在则覆盖
    existing = db.query(Document).filter(Document.case_id == case_id, Document.doc_type == doc_type).first()

    # 保存文件
    suffix = Path(file.filename).suffix if file.filename else ".pdf"
    stored_name = f"{case_id}_{doc_type}_{uuid.uuid4().hex[:8]}{suffix}"
    stored_path = UPLOAD_DIR / stored_name

    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="文件过大（最大50MB）")

    with open(stored_path, "wb") as f:
        f.write(content)

    if existing:
        # 覆盖旧文件
        old_path = Path(existing.stored_path) if existing.stored_path else None
        if old_path and old_path.exists():
            old_path.unlink()
        existing.original_filename = file.filename
        existing.stored_path = str(stored_path)
        existing.extracted_text = None
        doc = existing
    else:
        doc = Document(
            case_id=case_id,
            doc_type=doc_type,
            original_filename=file.filename,
            stored_path=str(stored_path),
        )
        db.add(doc)

    db.commit()
    db.refresh(doc)

    # 后台解析（避免大文件解析超时/崩溃阻塞上传）
    if background_tasks:
        background_tasks.add_task(_parse_in_background, doc.id, str(stored_path), doc_type, case_id)

    return {
        "id": doc.id,
        "doc_type": doc.doc_type,
        "original_filename": doc.original_filename,
        "extracted_text": "",
        "extracted_text_length": 0,
        "message": "上传成功，正在后台解析...",
        "auto_filled": {},
    }


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {
        "id": doc.id,
        "doc_type": doc.doc_type,
        "original_filename": doc.original_filename,
        "extracted_text": doc.extracted_text,
    }


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.stored_path:
        p = Path(doc.stored_path)
        if p.exists():
            p.unlink()
    db.delete(doc)
    db.commit()
    return {"message": "已删除"}
