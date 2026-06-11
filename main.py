import uvicorn
import traceback
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from pathlib import Path
import os

from config import HOST, PORT
from database import init_db, SessionLocal, engine
from models import Case, Document, Comparison, GeneratedDocument
from routers import auth, cases, documents, comparisons, analysis


def migrate_db():
    """自动迁移：为已存在的表添加新列"""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    # 为 cases 表添加 verified_chart_data 列（如果不存在）
    try:
        cols = [c["name"] for c in inspector.get_columns("cases")]
        if "verified_chart_data" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE cases ADD COLUMN verified_chart_data TEXT"))
                conn.commit()
                print("[migrate] cases.verified_chart_data 列已添加")
    except Exception as e:
        print(f"[migrate] 跳过: {e}")

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    migrate_db()
    print(f"\n{'='*50}")
    print(f"  审查意见答复系统已启动")
    print(f"  HOST={HOST}  PORT={PORT}")
    print(f"{'='*50}\n")
    yield


app = FastAPI(title="审查意见答复系统", version="1.0", lifespan=lifespan)

# 全局异常处理：确保任何未捕获异常都返回 JSON 而非空响应
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务器内部错误: {str(exc)}"},
    )

# 注册路由
app.include_router(auth.router)
app.include_router(cases.router)
app.include_router(documents.router)
app.include_router(comparisons.router)
app.include_router(analysis.router)

# 静态文件
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# 首页
@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "templates" / "index.html"))


@app.post("/api/admin/reset")
def reset_database():
    """清空所有数据（案件、文档、比对表、生成文档）"""
    db = SessionLocal()
    try:
        count_cases = db.query(Case).count()
        count_docs = db.query(Document).count()
        count_comps = db.query(Comparison).count()
        count_gen = db.query(GeneratedDocument).count()
        db.query(GeneratedDocument).delete()
        db.query(Comparison).delete()
        db.query(Document).delete()
        db.query(Case).delete()
        db.commit()
        return {
            "ok": True,
            "message": "数据库已清空",
            "deleted": {
                "cases": count_cases,
                "documents": count_docs,
                "comparisons": count_comps,
                "generated_docs": count_gen,
            }
        }
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": f"清库失败: {str(e)}"})
    finally:
        db.close()


@app.delete("/api/admin/delete-case/{case_id}")
def delete_single_case(case_id: int):
    """删除单个案件及其关联数据"""
    db = SessionLocal()
    try:
        case = db.query(Case).filter(Case.id == case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail="案件不存在")
        db.delete(case)
        db.commit()
        return {"ok": True, "message": "案件已删除"}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": str(e)})
    finally:
        db.close()


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
