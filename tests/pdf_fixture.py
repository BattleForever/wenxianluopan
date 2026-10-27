"""Small synthetic PDF writer for tests, backed by ReportLab (BSD license).

The geometry is top-origin to keep existing test layouts readable. Test PDFs
are parsed by the real production reader, not by this fixture writer.
"""
import io
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

FONTS={'helv':'Helvetica','hebo':'Helvetica-Bold','tiro':'Times-Roman'}

class Rect:
    def __init__(self,x0,y0,x1,y1):
        self.x0,self.y0,self.x1,self.y1=x0,y0,x1,y1

class Page:
    def __init__(self,width=595,height=842):
        self.width,self.height,self.ops=width,height,[]

    def insert_text(self,point,text,fontsize=11,fontname='helv',**_):
        self.ops.append((point[0],point[1],str(text),fontsize,FONTS.get(fontname,fontname)))

    def insert_textbox(self,rect,text,fontsize=11,fontname='helv',**_):
        font=FONTS.get(fontname,fontname);y=rect.y0+fontsize;lines=[]
        for paragraph in str(text).split('\n'):
            line=''
            for word in paragraph.split():
                candidate=(line+' '+word).strip()
                if line and stringWidth(candidate,font,fontsize)>rect.x1-rect.x0:
                    lines.append(line);line=word
                else:line=candidate
            lines.append(line)
        if y+max(0,len(lines)-1)*fontsize*1.2>rect.y1:
            return -1
        for line in lines:
            self.ops.append((rect.x0,y,line,fontsize,font));y+=fontsize*1.2
        return rect.y1-y

class Document:
    def __init__(self):self.pages=[]
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def new_page(self,width=595,height=842):
        page=Page(width,height);self.pages.append(page);return page
    def tobytes(self):
        buffer=io.BytesIO();out=canvas.Canvas(buffer,invariant=1,pageCompression=1)
        out.setAuthor('');out.setCreator('Synthetic PDF fixture')
        for page in self.pages:
            out.setPageSize((page.width,page.height))
            for x,y,text,size,font in page.ops:
                out.setFont(font,size);out.drawString(x,page.height-y,text)
            out.showPage()
        out.save();return buffer.getvalue()
    def save(self,path):
        from pathlib import Path
        Path(path).write_bytes(self.tobytes())
    def close(self):pass

def document():return Document()
