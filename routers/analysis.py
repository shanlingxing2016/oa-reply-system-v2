from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db
from models import Case, Document, Comparison
from services import ai_service as ai

router = APIRouter()

class AttackDefenseRequest(BaseModel):
    case_id: int
    round_num: int = 1
    previous_results: list = []


@router.post("/attack-defense-review")
def attack_defense_review(body: AttackDefenseRequest, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.id == body.case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    docs = db.query(Document).filter(Document.case_id == case.id).all()
    comp_rows = db.query(Comparison).filter(Comparison.case_id == case.id).order_by(Comparison.row_order).all()

    case_data = {
        "documents": [{"original_filename": d.original_filename, "parsed_text": d.parsed_text} for d in docs],
        "rejection_reasons": case.rejection_reasons or "",
        "strategies": case.strategies or "",
        "diff_features": "\n".join(
            f"特征{r.row_order}: {r.claim_feature or ''} | D1: {r.d1_feature or ''} | 区别: {r.diff_feature or ''}"
            for r in comp_rows if r.diff_feature
        ),
        "effect_analysis": case.effect_analysis or "",
    }

    result = ai.attack_defense_review(case_data, round_num=body.round_num, previous_results=body.previous_results)
    return result
