from pathlib import Path
from typing import Dict
import base64
import io
import struct

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}


class PDFParser:
    """PDF、Word 和图片文档解析，支持扫描件 OCR 回退及图表数据提取"""

    def __init__(self):
        pass

    def parse(self, file_path: str) -> Dict:
        ext = Path(file_path).suffix.lower()
        if ext == ".pdf":
            return self.parse_pdf(file_path)
        elif ext in (".docx",):
            return self.parse_docx(file_path)
        elif ext in IMAGE_EXTS:
            return self.parse_image(file_path)
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

    def parse_pdf(self, file_path: str) -> Dict:
        # 策略：PyMuPDF（最强文本提取 + 表格）→ pypdf → 图片OCR
        pages = []
        full_text = ""

        # 第一优先：PyMuPDF 提取（含表格结构，极低内存）
        fitz_text = self._fitz_extract_text(file_path)
        if fitz_text.strip():
            full_text = fitz_text.strip()
            # 按页拆分
            for i, block in enumerate(full_text.split("\f")):
                if block.strip():
                    pages.append({"page": i + 1, "content": block.strip()})
            total_pages = len(pages) or 1
            return {"total_pages": total_pages, "full_text": full_text, "pages": pages}

        # 第二优先：pypdf 文本提取
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append({"page": i + 1, "content": text})
        full_text = "\n\n".join(p["content"] for p in pages) if pages else ""

        # 第三：图片 OCR（扫描件专用，内存重）
        if not full_text.strip():
            full_text = self._ocr_pdf(file_path)
            pages = [{"page": 1, "content": full_text.strip()}] if full_text.strip() else []
            total_pages = len(reader.pages)
        else:
            total_pages = len(reader.pages)

        return {"total_pages": total_pages, "full_text": full_text.strip(), "pages": pages}

    def _append_page_image_ocr(self, pdf_path: str, pages: list) -> list:
        """遍历每页，提取嵌入图片并 OCR，将结果追加到对应页面内容中"""
        try:
            import fitz
            from PIL import Image

            doc = fitz.open(pdf_path)
            try:
                for page_info in pages:
                    page_idx = page_info.get("page", 1) - 1
                    if page_idx >= len(doc):
                        continue
                    page = doc[page_idx]
                    img_list = page.get_images(full=True)
                    if not img_list:
                        continue

                    page_images = []
                    for img_index, img in enumerate(img_list, start=1):
                        xref = img[0]
                        try:
                            base_image = doc.extract_image(xref)
                            image_bytes = base_image["image"]
                            pil_img = Image.open(io.BytesIO(image_bytes))
                            if pil_img.mode != "RGB":
                                pil_img = pil_img.convert("RGB")
                            page_images.append(pil_img)
                        except Exception:
                            continue

                    if not page_images:
                        continue

                    # 将所有图片拼接为一张，避免多次 API 调用
                    if len(page_images) == 1:
                        combined = page_images[0]
                    else:
                        max_w = max(im.size[0] for im in page_images)
                        total_h = sum(im.size[1] for im in page_images)
                        combined = Image.new("RGB", (max_w, total_h), "white")
                        y = 0
                        for im in page_images:
                            combined.paste(im, (0, y))
                            y += im.size[1]

                    img_text = self._ocr_image_content(combined)
                    if img_text and not img_text.startswith("["):
                        page_info["content"] += f"\n\n[图片/图表识别]\n{img_text}"
            finally:
                doc.close()
        except Exception:
            pass
        return pages

    def _fitz_extract_text(self, pdf_path: str) -> str:
        """用 PyMuPDF 提取 PDF 文本，自动识别表格结构并转为 Markdown 格式"""
        try:
            import fitz
            doc = fitz.open(pdf_path)
            try:
                MAX_PAGES = 50
                parts = []
                for i, page in enumerate(doc):
                    if i >= MAX_PAGES:
                        break
                    page_text = self._extract_page_with_tables(page)
                    if page_text.strip():
                        parts.append(page_text.strip())
                return "\f".join(parts)
            finally:
                doc.close()
        except Exception:
            return ""

    def _extract_page_with_tables(self, page) -> str:
        """提取单页文本，自动识别表格并转为 Markdown 表格格式"""
        try:
            import fitz

            # 尝试用 find_tables() 识别表格（PyMuPDF 1.23+ 支持）
            page_text = page.get_text("text")
            try:
                tabs = page.find_tables()
                if tabs.tables:
                    # 有表格：将页面内容分为"表格"和"普通文本"两部分分别处理
                    # 先获取所有表格的 bbox 范围
                    table_bboxes = [fitz.Rect(t.bbox) for t in tabs.tables]

                    # 用 blocks 模式提取文字块，过滤掉表格区域内的文字
                    blocks = page.get_text("blocks")
                    non_table_lines = []
                    for b in blocks:
                        bx0, by0, bx1, by1, text, *_ = b
                        block_rect = fitz.Rect(bx0, by0, bx1, by1)
                        in_table = any(block_rect.intersects(tbr) for tbr in table_bboxes)
                        if not in_table and text.strip():
                            non_table_lines.append((by0, text.strip()))

                    # 对非表格文字按 y 坐标排序
                    non_table_lines.sort(key=lambda x: x[0])

                    # 构建表格的 y 坐标（用于插入位置）
                    table_entries = []
                    for t in tabs.tables:
                        try:
                            rows = t.extract()
                            md = self._table_to_markdown(rows)
                            table_entries.append((t.bbox[1], md))  # bbox[1] = top y
                        except Exception:
                            pass

                    # 合并所有元素，按 y 坐标排序
                    all_elements = [(y, "TEXT", txt) for y, txt in non_table_lines]
                    all_elements += [(y, "TABLE", md) for y, md in table_entries]
                    all_elements.sort(key=lambda x: x[0])

                    result_parts = []
                    for _, kind, content in all_elements:
                        result_parts.append(content)
                    return "\n\n".join(result_parts)

            except AttributeError:
                # PyMuPDF 版本较旧，不支持 find_tables()，回退到自定义表格识别
                pass

            # 回退方案：用 blocks 模式按位置重构表格
            return self._extract_with_blocks(page)

        except Exception:
            return page.get_text("text")

    def _table_to_markdown(self, rows: list) -> str:
        """将表格行列数据转为 Markdown 表格字符串"""
        if not rows:
            return ""
        # 过滤全空行
        rows = [[str(cell or "").strip() for cell in row] for row in rows]
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            return ""

        # 确保列数一致
        max_cols = max(len(row) for row in rows)
        rows = [row + [""] * (max_cols - len(row)) for row in rows]

        lines = []
        header = rows[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    def _extract_with_blocks(self, page) -> str:
        """用 blocks 模式提取，按行列位置尝试还原表格结构（兼容旧版 PyMuPDF）"""
        blocks = page.get_text("blocks")
        if not blocks:
            return page.get_text("text")

        # 按 y 坐标（行）和 x 坐标（列）分组，检测疑似表格区域
        from collections import defaultdict
        import math

        y_groups = defaultdict(list)
        for b in blocks:
            x0, y0, x1, y1, text, *_ = b
            if text.strip():
                # 将 y 坐标取整到最近 10px，模拟同一行
                y_key = round(y0 / 10) * 10
                y_groups[y_key].append((x0, text.strip()))

        lines = []
        for y_key in sorted(y_groups.keys()):
            row_items = sorted(y_groups[y_key], key=lambda x: x[0])
            if len(row_items) > 1:
                # 多列：疑似表格行，用 tab 分隔
                lines.append("\t".join(item[1] for item in row_items))
            else:
                lines.append(row_items[0][1])

        return "\n".join(lines)

    def _extract_images_from_pdf(self, pdf_path: str) -> list:
        """从 PDF 提取每页为 Pillow Image，不依赖 poppler"""
        from pypdf import PdfReader
        from PIL import Image

        reader = PdfReader(pdf_path)
        images = []

        for i, page in enumerate(reader.pages):
            # 方法1: 尝试从页面资源中提取嵌入图片
            page_images = []
            try:
                if '/XObject' in page.get('/Resources', {}):
                    xobjects = page['/Resources']['/XObject'].get_object()
                    for obj_name in xobjects:
                        xobj = xobjects[obj_name].get_object()
                        if xobj.get('/Subtype') == '/Image':
                            img = self._xobject_to_pil(xobj)
                            if img:
                                page_images.append(img)
            except Exception:
                pass

            if page_images:
                # 如果有多张图片，拼接在一起
                if len(page_images) == 1:
                    images.append(page_images[0])
                else:
                    # 按宽度或高度排序后拼接
                    page_images.sort(key=lambda im: (im.size[1], im.size[0]), reverse=True)
                    total_height = sum(im.size[1] for im in page_images)
                    max_width = max(im.size[0] for im in page_images)
                    combined = Image.new('RGB', (max_width, total_height), 'white')
                    y_offset = 0
                    for im in page_images:
                        combined.paste(im, (0, y_offset))
                        y_offset += im.size[1]
                    images.append(combined)
            else:
                # 方法2: 如果没有嵌入图片，将页面渲染为图片（简易方法）
                img = self._render_page_simple(page, i)
                if img:
                    images.append(img)

        return images

    def _xobject_to_pil(self, xobj) -> 'Image.Image | None':
        """将 PDF XObject 转换为 PIL Image"""
        from PIL import Image

        try:
            width = int(xobj['/Width'])
            height = int(xobj['/Height'])
            color_space = xobj.get('/ColorSpace', '/DeviceRGB')

            # 获取原始图像数据
            data = xobj.get_data()

            if xobj.get('/Filter') in ('/DCTDecode', '/JPXDecode'):
                # JPEG 或 JPEG2000 格式
                img = Image.open(io.BytesIO(data))
                return img.convert('RGB') if img.mode != 'RGB' else img
            elif xobj.get('/Filter') == '/FlateDecode':
                # 解压缩的原始像素数据
                if isinstance(color_space, str) and color_space.startswith('/'):
                    color_space = color_space[1:]
                else:
                    color_space = str(color_space)

                # 处理颜色空间
                n_components = 3  # default RGB
                if color_space == 'DeviceGray':
                    n_components = 1
                elif color_space == 'DeviceCMYK':
                    n_components = 4
                elif color_space == 'DeviceRGB':
                    n_components = 3

                # 处理预测器
                predictor = int(xobj.get('/DecodeParms', {}).get('/Predictor', 1)) if hasattr(xobj.get('/DecodeParms'), 'get') else 1

                if predictor == 1:
                    img = Image.frombytes('RGB' if n_components == 3 else 'L', (width, height), data)
                else:
                    # PNG 预测器，行内处理
                    try:
                        from zlib import decompress
                        # 已经是解压后的数据，按行处理 PNG 预测
                        row_size = width * n_components + 1
                        raw_data = bytearray()
                        for j in range(0, len(data), row_size):
                            row = data[j:j + row_size]
                            if row:
                                filter_type = row[0]
                                pixel_data = row[1:]
                                if filter_type == 0:  # None
                                    raw_data.extend(pixel_data)
                                elif filter_type == 1:  # Sub
                                    prev = bytearray(len(pixel_data))
                                    for k in range(len(pixel_data)):
                                        raw_byte = pixel_data[k]
                                        decoded = (raw_byte + prev[k]) & 0xFF
                                        prev[k] = decoded
                                        raw_data.append(decoded)
                                elif filter_type == 2:  # Up
                                    for k in range(len(pixel_data)):
                                        raw_data.append(pixel_data[k])
                                else:
                                    raw_data.extend(pixel_data)
                        img = Image.frombytes('RGB' if n_components == 3 else 'L', (width, height), bytes(raw_data))
                    except Exception:
                        return None

                if n_components == 1:
                    img = img.convert('RGB')
                return img
            else:
                # 其他格式，尝试直接打开
                return Image.open(io.BytesIO(data)).convert('RGB')
        except Exception:
            return None

    def _render_page_simple(self, page, page_num: int) -> 'Image.Image | None':
        """简化的页面渲染（无 poppler 时的后备方案）"""
        # 没有图片也没有文字的页面，返回空白
        return None

    def parse_image(self, file_path: str) -> Dict:
        """解析独立图片文件（JPG/PNG 等），支持 OCR 文字和图表数据提取"""
        from PIL import Image
        img = Image.open(file_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        text = self._ocr_image_content(img)
        return {"total_pages": 1, "full_text": text, "pages": [{"page": 1, "content": text}]}

    def _preprocess_image(self, img) -> 'Image.Image':
        """图片预处理：增强对比度、锐化，提升OCR准确性"""
        from PIL import ImageEnhance, ImageFilter

        # 1. 自动对比度增强
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.3)

        # 2. 锐化（增强文字/线条边缘）
        img = img.filter(ImageFilter.SHARPEN)

        # 3. 亮度微调
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(1.05)

        return img

    def _ocr_prompt(self) -> str:
        return (
            "你是一位专业的科研图表分析专家。这张图片是科研论文/专利中的附图。\n"
            "请严格按照以下步骤分析，一步一步来，不要跳过任何步骤：\n\n"
            "=== 步骤1：描述图表整体结构 ===\n"
            "先告诉我这是什么类型的图表（柱状图/折线图/饼图/表格/电泳图/照片等），\n"
            "然后描述：\n"
            "- 图表标题是什么\n"
            "- X轴代表什么，有哪些类别/标签\n"
            "- Y轴代表什么，单位和刻度范围\n"
            "- 有多少个数据系列/柱子/折线\n\n"
            "=== 步骤2：逐个读取每个数据点（最重要的步骤） ===\n"
            "请从左到右（或从上到下）逐个读取每个数据点的数值。\n"
            "方法：\n"
            "1. 先看Y轴刻度，确认每个刻度间隔代表多少\n"
            "2. 对每一个柱子/数据点，仔细看它的高度/位置对应Y轴的哪个刻度\n"
            "3. 如果柱顶或数据点旁边有数字标注，直接读取标注值\n"
            "4. 如果没有标注，根据柱顶在Y轴上的投影位置估算数值\n"
            "5. 估算时以Y轴刻度为基准，给出合理估计值（如25.0、26.5等）\n"
            "6. 必须逐个列出所有数据点，不能遗漏任何一个\n\n"
            "=== 步骤3：输出数据表格 ===\n"
            "将步骤2的结果整理为Markdown表格：\n"
            "| 序号 | X轴类别/名称 | 数值 | 备注（有无标注/估算） |\n"
            "|---|---|---|---|\n"
            "| 1 | ... | ... | ... |\n"
            "...\n\n"
            "=== 步骤4：补充文字信息 ===\n"
            "提取图片中所有其他可见文字：\n"
            "- 图注/说明文字\n"
            "- 统计显著性标记（如 *、**、# 等）\n"
            "- 任何注释或脚注\n\n"
            "=== 重要规则 ===\n"
            "- 你必须逐个数据点读取，不能概括或跳过\n"
            "- 如果某个值确实看不清，写'约XX'或'无法辨认'\n"
            "- 严禁编造数据，不确定就写'无法辨认'\n"
            "- 不要输出任何总结、分析或建议，只输出上述结构化内容"
        )

    def _ocr_image_content(self, img) -> str:
        """对单张 PIL Image 调用多模态 API 进行 OCR，支持文字、表格和图表数据提取。
        关键修复：使用配置中的OCR模型（deepseek-v4-pro 而非 deepseek-chat），大幅提升图表识别能力。"""
        try:
            from config import (
                DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
                DEEPSEEK_OCR_MODEL, DEEPSEEK_MODEL
            )
            from openai import OpenAI
            from PIL import Image

            if not DEEPSEEK_API_KEY:
                return ""

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

            # 使用配置中的OCR模型（默认 deepseek-v4-pro，比 deepseek-chat 多模态能力强很多）
            ocr_model = DEEPSEEK_OCR_MODEL or DEEPSEEK_MODEL or "deepseek-v4-pro"

            # 预处理：增强对比度、锐化
            img = self._preprocess_image(img)

            # 压缩：max 1800px, JPEG quality 90（极限质量，确保数值可读）
            w, h = img.size
            if w > 1800:
                ratio = 1800 / w
                img = img.resize((1800, int(h * ratio)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode()

            content = [
                {"type": "text", "text": self._ocr_prompt()},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]

            try:
                resp = client.chat.completions.create(
                    model=ocr_model,
                    messages=[{"role": "user", "content": content}],
                    temperature=0.0,   # 最低温度，最大限度减少幻觉
                    max_tokens=12000,
                    timeout=120,
                )
                raw = resp.choices[0].message.content or ""
                return self._parse_ocr_response(raw)
            except Exception as e:
                return f"[API调用失败: {str(e)[:100]}]"

        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"[OCR解析异常: {str(e)[:200]}]"

    def _parse_ocr_response(self, raw: str) -> str:
        """解析模型返回的结构化OCR结果，清洗步骤标题，保留有用的数据和文字。"""
        lines = raw.strip().split("\n")
        result_parts = []
        skip_patterns = (
            "=== 步骤", "=== 重要规则 ===", "方法：", "1. 先", "2. 对", "3. 如果",
            "4. 如果", "5. 估算", "6. 必须", "- 你必", "- 如果", "- 严禁", "- 不要",
        )

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # 跳过步骤说明和规则行
            if any(stripped.startswith(p) for p in skip_patterns):
                continue
            result_parts.append(stripped)

        return "\n".join(result_parts).strip()

    @staticmethod
    def extract_data_from_patent_text(patent_text: str) -> str:
        """从专利文字中提取实验数据描述，作为图表OCR的补充/校验来源。\n
        专利实施例通常会以文字形式描述图表数据（如\"PP-10菌株的DPPH清除率为25.2%\"），\n        这些文字描述往往比从图片OCR读取更准确。\n        """
        import re
        if not patent_text:
            return ""

        # 查找实施例中提到的图表数据模式
        patterns = [
            # 匹配 "XX为XX%"、"XX达到XX%" 等数据描述
            r"[^。，；\n]{0,30}(?:为|达到|约为|分别是|依次为|分别达到了|高达)[^。，；\n]{0,50}(?:\d+\.?\d*)\s*%[^。，；\n]{0,30}",
            # 匹配 "清除率|活性|含量|浓度|效率" 等关键词附近的数据
            r"(?:清除率|活性|含量|浓度|效率|抑制率|存活率|增长率|OD值|吸光度)[^。，；\n]{0,40}(?:\d+\.?\d*)\s*%?[^。，；\n]{0,20}",
            # 匹配 "图\d+显示|如图\d+所示" 附近的数据
            r"(?:图\d+[显示表明]|如图\d+所示)[^。\n]{0,80}(?:\d+\.?\d*)\s*%?[^。\n]{0,40}",
            # 匹配带单位的数值描述
            r"[^。，；\n]{0,20}(?:\d+\.?\d*)\s*(?:%|mg/mL|U/mL|μg/mL|mmol/L|g/L|h|小时|天|min)[^。，；\n]{0,30}",
        ]

        findings = []
        for pattern in patterns:
            matches = re.findall(pattern, patent_text, re.IGNORECASE)
            for m in matches:
                m_clean = m.strip()
                if m_clean and m_clean not in findings and len(m_clean) > 5:
                    findings.append(m_clean)

        if not findings:
            return ""

        # 去重并按在原文中出现顺序排序
        seen = set()
        ordered = []
        for f in findings:
            key = re.sub(r"\s+", "", f)
            if key not in seen:
                seen.add(key)
                ordered.append(f)

        return "[从专利文字中提取的实验数据参考]\n" + "\n".join(f"- {o}" for o in ordered[:30])

    def _ocr_pdf(self, pdf_path: str) -> str:
        """用 DeepSeek 多模态 API 对扫描件 PDF 做 OCR
        关键优化：流式处理，每批提取→OCR→释放内存，避免 OOM"""
        try:
            from pypdf import PdfReader

            reader = PdfReader(pdf_path)
            total_pages = len(reader.pages)
            MAX_PAGES = min(total_pages, 30)  # 最多30页

            all_text = []

            # 流式处理：每次只从 PDF 中提取 1 页，OCR后立即释放
            BATCH_SIZE = 1  # Render 512MB 下每批仅 1 页
            for batch_start in range(0, MAX_PAGES, BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, MAX_PAGES)

                page_texts = []
                for page_idx in range(batch_start, batch_end):
                    page = reader.pages[page_idx]
                    img = self._render_page_to_image(page, page_idx, pdf_path)
                    if img is None:
                        continue
                    page_texts.append(self._ocr_image_content(img))

                # 如果这批没有任何图片（也没有文字），跳过
                if not page_texts:
                    all_text.append(f"--- 第{batch_start + 1}-{batch_end}页 ---\n\n（无可提取文字）")
                    continue

                combined = "\n\n".join(page_texts)
                all_text.append(f"--- 第{batch_start + 1}-{batch_end}页 ---\n\n{combined}")

            return "\n\n".join(all_text)

        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"[OCR解析异常: {str(e)[:200]}]"

    def _render_page_to_image(self, page, page_num: int, pdf_path: str = "") -> 'Image.Image | None':
        """将单页 PDF 渲染为 PIL Image。
        方法1: pypdf 提取嵌入图片
        方法2: PyMuPDF(fitz) 原生渲染（逐页，不预加载，内存友好）"""
        from PIL import Image

        # 方法1: 提取嵌入图片
        try:
            if '/XObject' in page.get('/Resources', {}):
                xobjects = page['/Resources']['/XObject'].get_object()
                page_images = []
                for obj_name in xobjects:
                    xobj = xobjects[obj_name].get_object()
                    if xobj.get('/Subtype') == '/Image':
                        img = self._xobject_to_pil(xobj)
                        if img:
                            page_images.append(img)
                if page_images:
                    if len(page_images) == 1:
                        return page_images[0]
                    # 多图拼接
                    page_images.sort(key=lambda im: im.size[1], reverse=True)
                    total_h = sum(im.size[1] for im in page_images)
                    max_w = max(im.size[0] for im in page_images)
                    combined = Image.new('RGB', (max_w, total_h), 'white')
                    y = 0
                    for im in page_images:
                        combined.paste(im, (0, y))
                        y += im.size[1]
                    return combined
        except Exception:
            pass

        # 方法2: PyMuPDF 原生渲染（能处理矢量PDF、扫描件等任何格式）
        if pdf_path:
            try:
                return self._fitz_render_page(pdf_path, page_num)
            except Exception:
                pass

        return None

    def _fitz_render_page(self, pdf_path: str, page_num: int) -> 'Image.Image | None':
        """用 PyMuPDF 渲染单页（按需打开文件，用完立即关闭）
        使用 200 DPI 渲染，确保图表中的细小文字和数值清晰可读。"""
        import fitz
        from PIL import Image

        doc = fitz.open(pdf_path)
        try:
            if page_num >= len(doc):
                return None
            page = doc[page_num]
            # 渲染为 200 DPI（高于普通OCR的150 DPI，确保图表数值清晰）
            mat = fitz.Matrix(200 / 72, 200 / 72)
            pix = page.get_pixmap(matrix=mat, colorspace="rgb")
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            return img
        finally:
            doc.close()

    def extract_pdf_images_text(self, pdf_path: str, max_images: int = 2) -> str:
        """安全提取PDF中嵌入图片的文字/图表数据（AI分析阶段调用）。
        限制：只取面积最大的前 max_images 张图片进行OCR，防止超时崩溃。
        过滤掉过小的图片（图标、装饰线等）。"""
        try:
            import fitz
            from PIL import Image
        except Exception:
            return ""

        # 第1步：扫描所有页，收集所有候选图片及其元信息
        candidates = []  # [(area, page_idx, pil_img), ...]
        try:
            doc = fitz.open(pdf_path)
            try:
                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    img_list = page.get_images(full=True)
                    if not img_list:
                        continue
                    for img in img_list:
                        xref = img[0]
                        try:
                            base_image = doc.extract_image(xref)
                            pil_img = Image.open(io.BytesIO(base_image["image"]))
                            area = pil_img.size[0] * pil_img.size[1]
                            # 过滤过小图片（图标、装饰线）
                            if area >= 80000:  # 约 280x280
                                candidates.append((area, page_idx, pil_img))
                        except Exception:
                            continue
            finally:
                doc.close()
        except Exception:
            return ""

        if not candidates:
            return ""

        # 按面积降序，只取前 max_images 张
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = candidates[:max_images]

        results = []
        for area, page_idx, pil_img in selected:
            if pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
            try:
                img_text = self._ocr_image_content(pil_img)
                if img_text and not img_text.startswith("["):
                    results.append(f"--- 第{page_idx + 1}页图片/图表 ---\n{img_text}")
            except Exception:
                continue

        return "\n\n".join(results) if results else ""

    def parse_docx(self, file_path: str) -> Dict:
        import docx2txt
        text = docx2txt.process(file_path)
        return {"total_pages": 1, "full_text": text.strip(), "pages": [{"page": 1, "content": text.strip()}]}
