from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from models import Case, Document, Comparison, GeneratedDocument

router = APIRouter(prefix="/api/cases", tags=["cases"])


class CaseCreate(BaseModel):
    case_number: str
    case_name: Optional[str] = ""
    case_type: Optional[str] = "第一次审查意见"


class CaseUpdate(BaseModel):
    case_number: Optional[str] = None
    case_name: Optional[str] = None
    case_type: Optional[str] = None
    status: Optional[str] = None
    current_step: Optional[int] = None
    rejection_reasons: Optional[str] = None
    ai_summary: Optional[str] = None
    selected_strategy: Optional[str] = None
    agent_notes: Optional[str] = None


def _case_to_dict(c: Case) -> dict:
    return {
        "id": c.id,
        "case_number": c.case_number,
        "case_name": c.case_name,
        "case_type": c.case_type,
        "status": c.status,
        "current_step": c.current_step,
        "rejection_reasons": c.rejection_reasons,
        "ai_summary": c.ai_summary,
        "selected_strategy": c.selected_strategy,
        "agent_notes": c.agent_notes,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "document_count": len(c.documents) if hasattr(c, "documents") else 0,
    }


@router.get("")
def list_cases(
    search: str = "",
    status: str = "",
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
):
    q = db.query(Case)
    if search:
        q = q.filter(
            (Case.case_number.contains(search)) | (Case.case_name.contains(search))
        )
    if status:
        q = q.filter(Case.status == status)
    total = q.count()
    items = q.order_by(Case.updated_at.desc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [_case_to_dict(c) for c in items]}


@router.post("")
def create_case(body: CaseCreate, db: Session = Depends(get_db)):
    existing = db.query(Case).filter(Case.case_number == body.case_number).first()
    if existing:
        raise HTTPException(status_code=400, detail="该申请号已存在")
    c = Case(case_number=body.case_number, case_name=body.case_name, case_type=body.case_type)
    db.add(c)
    db.commit()
    db.refresh(c)
    return _case_to_dict(c)


@router.get("/{case_id}")
def get_case(case_id: int, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="案件不存在")
    result = _case_to_dict(c)
    # 附加文档
    result["documents"] = [
        {
            "id": d.id,
            "doc_type": d.doc_type,
            "original_filename": d.original_filename,
            "extracted_text": d.extracted_text[:500] if d.extracted_text else None,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in c.documents
    ]
    # 附加比对数据
    result["comparisons"] = {
        "table1": [
            {
                "id": r.id,
                "claim": r.claim,
                "feature": r.feature,
                "ref_position": r.ref_position,
                "ref_content": r.ref_content,
                "pub_status": r.pub_status,
                "analysis": r.analysis,
                "sort_order": r.sort_order,
            }
            for r in sorted(
                [x for x in c.comparisons if x.table_type == "table1"],
                key=lambda x: x.sort_order or 0,
            )
        ],
        "table2": [
            {
                "id": r.id,
                "diff_no": r.diff_no,
                "feature": r.feature,
                "ref_document": r.ref_document,
                "ref_position": r.ref_position,
                "ref_content": r.ref_content,
                "pub_status": r.pub_status,
                "analysis": r.analysis,
                "sort_order": r.sort_order,
            }
            for r in sorted(
                [x for x in c.comparisons if x.table_type == "table2"],
                key=lambda x: x.sort_order or 0,
            )
        ],
        "effect": [
            {
                "id": r.id,
                "feature": r.feature,
                "app_position": r.app_position,
                "ref_document": r.ref_document,
                "ref_position": r.ref_position,
                "ref_content": r.ref_content,
                "pub_status": r.pub_status,
                "analysis": r.analysis,
                "sort_order": r.sort_order,
            }
            for r in sorted(
                [x for x in c.comparisons if x.table_type == "effect"],
                key=lambda x: x.sort_order or 0,
            )
        ],
    }
    # 附加已生成的文档
    result["generated_docs"] = [
        {
            "id": g.id,
            "doc_content": g.doc_content,
            "strategy_used": g.strategy_used,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
        for g in c.generated_docs
    ]
    return result


@router.put("/{case_id}")
def update_case(case_id: int, body: CaseUpdate, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="案件不存在")
    # 如果修改申请号，检查是否重复
    if body.case_number and body.case_number != c.case_number:
        dup = db.query(Case).filter(Case.case_number == body.case_number, Case.id != case_id).first()
        if dup:
            raise HTTPException(status_code=400, detail="该申请号已被其他案件使用")
    update_data = body.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(c, k, v)
    c.updated_at = datetime.now()
    db.commit()
    db.refresh(c)
    return _case_to_dict(c)


@router.delete("/{case_id}")
def delete_case(case_id: int, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="案件不存在")
    db.delete(c)
    db.commit()
    return {"message": "已删除"}
