import uvicorn
import traceback
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from pathlib import Path
import os

from config import HOST, PORT
from database import init_db
from routers import auth, cases, documents, comparisons, analysis

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
