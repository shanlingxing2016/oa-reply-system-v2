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

    def _ocr_prompt(self) -> str:
        return (
            "请完整分析以下图片，并提取其中的所有信息：\n\n"
            "1. 文字内容：完整提取所有可见文字，保持原始排版和段落结构。\n"
            "2. 表格：如有表格，转换为 Markdown 表格格式（| 列1 | 列2 |，第二行用 |---|---| 分隔）。\n"
            "3. 图表数据（如柱状图、折线图、饼图、散点图、热力图等）：\n"
            "   - 提取图表标题\n"
            "   - 提取X轴、Y轴标签及单位\n"
            "   - 提取各数据系列的名称、颜色标识和对应数值\n"
            "   - 将数据整理为 Markdown 表格输出\n"
            "   - 如有图例，提取图例内容与对应数据系列\n"
            "4. 不要遗漏任何信息。直接输出提取结果，不要添加任何说明或总结。"
        )

    def _ocr_image_content(self, img) -> str:
        """对单张 PIL Image 调用 DeepSeek 多模态 API 进行 OCR，支持文字、表格和图表数据提取"""
        try:
            from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
            from openai import OpenAI
            from PIL import Image

            if not DEEPSEEK_API_KEY:
                return ""

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

            # 压缩：max 1200px（科研图表需要更高分辨率）, JPEG quality 40
            w, h = img.size
            if w > 1200:
                ratio = 1200 / w
                img = img.resize((1200, int(h * ratio)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=40, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode()

            content = [
                {"type": "text", "text": self._ocr_prompt()},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]

            try:
                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": content}],
                    temperature=0.1,
                    max_tokens=8000,
                    timeout=90,
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                return f"[API调用失败: {str(e)[:100]}]"

        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"[OCR解析异常: {str(e)[:200]}]"

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
        """用 PyMuPDF 渲染单页（按需打开文件，用完立即关闭）"""
        import fitz
        from PIL import Image

        doc = fitz.open(pdf_path)
        try:
            if page_num >= len(doc):
                return None
            page = doc[page_num]
            # 渲染为 150 DPI 的 pixmap（足够 OCR，不过大）
            mat = fitz.Matrix(150 / 72, 150 / 72)
            pix = page.get_pixmap(matrix=mat, colorspace="rgb")
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            return img
        finally:
            doc.close()

    def parse_docx(self, file_path: str) -> Dict:
        import docx2txt
        text = docx2txt.process(file_path)
        return {"total_pages": 1, "full_text": text.strip(), "pages": [{"page": 1, "content": text.strip()}]}
