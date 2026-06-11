from datetime import datetime
from sqlalchemy import Column, Integer, Text, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


class Case(Base):
    __tablename__ = "cases"

    id = Column(Integer, primary_key=True, index=True)
    case_number = Column(String(50), unique=True, nullable=False, index=True)
    case_name = Column(Text)
    case_type = Column(String(50), default="第一次审查意见")
    status = Column(String(20), default="draft")
    current_step = Column(Integer, default=1)
    rejection_reasons = Column(Text)  # JSON
    ai_summary = Column(Text)
    selected_strategy = Column(String(10))
    agent_notes = Column(Text)
    verified_chart_data = Column(Text)  # JSON: 人工校对后的图表数据
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    documents = relationship("Document", back_populates="case", cascade="all, delete-orphan")
    comparisons = relationship("Comparison", back_populates="case", cascade="all, delete-orphan")
    generated_docs = relationship("GeneratedDocument", back_populates="case", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    doc_type = Column(String(20), nullable=False)  # oa/patent/d1/d2
    original_filename = Column(Text)
    stored_path = Column(Text)
    extracted_text = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

    case = relationship("Case", back_populates="documents")


class Comparison(Base):
    __tablename__ = "comparisons"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    table_type = Column(String(20), nullable=False)  # table1/table2/effect
    sort_order = Column(Integer, default=0)

    # 表一（各权利要求 vs D1）& 表二 & 效果表通用
    claim = Column(String(10))
    feature = Column(Text)
    ref_position = Column(Text)
    ref_content = Column(Text)
    pub_status = Column(String(10), default="no")
    analysis = Column(Text)

    # 表二额外字段
    diff_no = Column(String(10))
    ref_document = Column(String(10))

    # 效果表额外字段
    app_position = Column(Text)
    app_value = Column(Text)   # 本申请具体数值（如"IC50=5.2nM"）
    ref_value = Column(Text)   # 对比文件具体数值（如"IC50=100nM"）

    created_at = Column(DateTime, default=datetime.now)

    case = relationship("Case", back_populates="comparisons")


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    doc_content = Column(Text)
    strategy_used = Column(String(10))
    created_at = Column(DateTime, default=datetime.now)

    case = relationship("Case", back_populates="generated_docs")
