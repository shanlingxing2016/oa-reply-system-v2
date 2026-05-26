from pathlib import Path
from typing import Dict
import base64
import io
import struct


class PDFParser:
    """PDF 和 Word 文档解析，支持扫描件 OCR 回退"""

    def __init__(self):
        pass

    def parse(self, file_path: str) -> Dict:
        ext = Path(file_path).suffix.lower()
        if ext == ".pdf":
            return self.parse_pdf(file_path)
        elif ext in (".docx",):
            return self.parse_docx(file_path)
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

    def parse_pdf(self, file_path: str) -> Dict:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append({"page": i + 1, "content": text})

        full_text = "\n\n".join(p["content"] for p in pages)

        # 如果 pypdf 提取不到文字，尝试 OCR
        if not full_text.strip():
            full_text = self._ocr_pdf(file_path)
            if full_text.strip():
                # OCR 成功，按页重新组织
                pages = []
                for i, block in enumerate(full_text.split("\n\n--- 第{}页 ---\n\n".format(""))):
                    if block.strip():
                        pages.append({"page": i + 1, "content": block.strip()})
                if not pages:
                    pages = [{"page": 1, "content": full_text.strip()}]
            total_pages = len(reader.pages)
        else:
            total_pages = len(reader.pages)

        return {"total_pages": total_pages, "full_text": full_text.strip(), "pages": pages}

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

    def _ocr_pdf(self, pdf_path: str) -> str:
        """用 DeepSeek 多模态 API 对扫描件 PDF 做 OCR（优化内存和时间）"""
        try:
            from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
            from openai import OpenAI
            from PIL import Image

            if not DEEPSEEK_API_KEY:
                return ""

            images = self._extract_images_from_pdf(pdf_path)
            if not images:
                return ""

            # 限制最多 30 页，避免超时
            MAX_PAGES = 30
            if len(images) > MAX_PAGES:
                images = images[:MAX_PAGES]

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
            all_text = []

            # 每次发送 5 张（压缩后），减少 API 调用次数
            batch_size = 5
            for batch_start in range(0, len(images), batch_size):
                batch_raw = images[batch_start:batch_start + batch_size]
                content = [{"type": "text", "text": "请完整提取以下文档图片中的所有文字内容，保持原始格式和段落结构。不要遗漏任何文字，包括页眉页脚。直接输出提取的文字，不要加任何说明。"}]
                for img in batch_raw:
                    # 压缩图片：最大 1200px 宽，quality 60，大幅减少 base64 体积
                    w, h = img.size
                    max_w = 1200
                    if w > max_w:
                        ratio = max_w / w
                        img = img.resize((max_w, int(h * ratio)), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=60, optimize=True)
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}"
                        }
                    })

                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": content}],
                    temperature=0.1,
                    max_tokens=8000,
                    timeout=60,  # 单批次超时 60 秒
                )
                page_text = resp.choices[0].message.content or ""
                all_text.append(f"--- 第{batch_start + 1}页 ---\n\n{page_text}")

            return "\n\n".join(all_text)

        except Exception:
            return ""

    def parse_docx(self, file_path: str) -> Dict:
        import docx2txt
        text = docx2txt.process(file_path)
        return {"total_pages": 1, "full_text": text.strip(), "pages": [{"page": 1, "content": text.strip()}]}
