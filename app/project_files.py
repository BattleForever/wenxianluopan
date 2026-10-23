"""Readable, regenerable local project indexes; original papers and user files stay intact."""
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import quote
import uuid
from .config import DATA_DIR, PORT
from .db import get_db
from .private_settings import atomic_json, read_json

LOCK=threading.Lock()
LABELS={'project':'项目文件','papers':'文献目录','originals':'原文索引','backups':'备份','exports':'导出','notes':'个人资料','system':'系统数据'}
STYLE='body{max-width:1080px;margin:42px auto;padding:0 24px;background:#0e1929;color:#deebf7;font:15px/1.85 system-ui,"Microsoft YaHei",sans-serif}a{color:#7bd7ca}h1{font-size:25px;line-height:1.5}h2{font-size:18px}p,small{color:#a8bfd5}.card,details{padding:18px 22px;border:1px solid #2b435c;border-radius:10px;margin:14px 0;background:#132338}summary{cursor:pointer}li{margin:9px 0}input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #3a5975;border-radius:6px;background:#0f2034;color:#e2edf8}pre{white-space:pre-wrap;font:inherit}.muted{font-size:12px;color:#86a1ba}.actions{display:flex;gap:20px;flex-wrap:wrap}'

def root():return DATA_DIR/'项目文件'

def directory(kind):
    if kind=='system':return DATA_DIR.resolve()
    if kind=='project':path=root()
    elif kind in LABELS:path=root()/LABELS[kind]
    else:raise ValueError('未知的项目目录。')
    if path.is_symlink() or root().is_symlink():raise ValueError('项目目录不能是符号链接。')
    path.mkdir(parents=True,exist_ok=True)
    return path

def ensure_tree():
    for kind in ('papers','originals','backups','exports','notes'):directory(kind)

def safe_name(value,limit=44):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',str(value)).strip(' .')
    return (value[:limit].rstrip(' .') or '未命名')

def write(path,content):
    if not path.resolve().is_relative_to(root().resolve()):raise ValueError('项目文件路径越界。')
    if any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(root())):raise ValueError('项目索引不能通过符号链接写入。')
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as out:out.write(content.encode('utf-8') if isinstance(content,str) else content)
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)

def save_export(stem,extension,content):
    path=directory('exports')/(safe_name(stem,52)+'_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:5]+'.'+extension)
    write(path,content)
    return path

def shell(title,body):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style><body>'+body+'</body></html>'

def file_hash(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def refresh():
    """The database owns classifications. Generated indexes never own an original PDF."""
    with LOCK:
        ensure_tree()
        with get_db() as db:
            db.execute('BEGIN')
            papers={r['id']:dict(r) for r in db.execute("SELECT * FROM papers WHERE id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library') ORDER BY id")}
            folders={r['id']:dict(r) for r in db.execute('SELECT * FROM organizer_folders')}
            memberships=[dict(r) for r in db.execute('SELECT * FROM organizer_items')]
            reading={r['paper_id']:dict(r) for r in db.execute("SELECT * FROM reading_list WHERE paper_id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='reading')")}
        base=root();manifest={};groups={};modified=0
        old=read_json(DATA_DIR/'private/project_files.manifest.json').get('files',{})
        def emit(path,content):
            nonlocal modified
            rel=path.relative_to(base).as_posix()
            expected=hashlib.sha256(content.encode('utf-8') if isinstance(content,str) else content).hexdigest()
            if path.is_file() and rel in old and file_hash(path)!=old[rel] and file_hash(path)!=expected:
                # Preserve user edits to a generated document before refreshing it.
                saved=directory('notes')/'保留的目录修改'/(path.stem+'_'+uuid.uuid4().hex[:7]+path.suffix)
                write(saved,path.read_bytes());modified+=1
            if path.exists() and rel not in old and file_hash(path)!=expected:
                saved=directory('notes')/'保留的目录修改'/(path.stem+'_'+uuid.uuid4().hex[:7]+path.suffix)
                write(saved,path.read_bytes());modified+=1
            write(path,content);manifest[rel]=file_hash(path)
        def folder_path(fid):
            names=[];seen=set()
            while fid in folders and fid not in seen:
                seen.add(fid);f=folders[fid];names.insert(0,safe_name(f['name'],16)+' ['+str(fid)+']');fid=f['parent_id']
            return names
        belongs={}
        for item in memberships:
            f=folders.get(item['folder_id'])
            if f:belongs.setdefault((item['paper_id'],f['scope']),[]).append(f['id'])
        for pid,paper in papers.items():
            title=paper.get('title_zh') or paper.get('title') or '未命名文献'
            original=paper.get('title') or title
            body='<h1>'+html.escape(title)+'</h1>'
            if original!=title:body+='<p>'+html.escape(original)+'</p>'
            body+='<p>'+html.escape(str(paper.get('year') or '年份未知'))+' · '+html.escape(paper.get('venue') or '期刊未记录')+'</p>'
            body+='<div class="actions"><a href="http://127.0.0.1:'+str(PORT)+'/?paper='+str(pid)+'">在文献罗盘中查看</a>'
            pdf=Path(paper.get('local_pdf_path') or '')
            if not pdf.is_absolute():pdf=DATA_DIR/pdf
            available=pdf.is_file() and pdf.suffix.lower()=='.pdf'
            if available:body+='<a href="'+html.escape(pdf.resolve().as_uri(),quote=True)+'">打开文献原文</a>'
            doi=paper.get('doi')
            if doi:body+='<a href="https://doi.org/'+quote(doi,safe='/')+'">出版页面</a>'
            body+='</div>'
            abstract=paper.get('abstract_zh') or paper.get('abstract')
            if abstract:body+='<section class="card"><h2>摘要</h2><pre>'+html.escape(abstract)+'</pre></section>'
            if pid in reading and reading[pid].get('note'):body+='<section class="card"><h2>阅读备注</h2><pre>'+html.escape(reading[pid]['note'])+'</pre></section>'
            body+='<p class="muted">此页根据软件记录生成。请在软件中修改分类和备注；原始文献文件未移动。关闭后台后仍可浏览本页和已存在的原文。</p>'
            content=shell(title,body);filename=safe_name(title)+' ['+str(pid)+'].html'
            scopes=[('library','文献库')] if paper.get('status')=='library' else [('candidate','待筛选文献')]
            if pid in reading:scopes.append(('reading','阅读清单'))
            for scope,label in scopes:
                ids=belongs.get((pid,scope),[])
                paths=[folder_path(fid) for fid in ids] or [['未分类']]
                for path in paths:
                    full_label=' / '.join([label,*path]);physical=path
                    if sum(map(len,path))>100:physical=['深层分类',path[-1]]
                    target=directory('papers')/label/Path(*physical)/filename
                    emit(target,content);groups.setdefault(full_label,[]).append((title,target.relative_to(base).as_posix()))
            if available:
                link=directory('originals')/(safe_name(title)+' ['+str(pid)+'].url')
                emit(link,'[InternetShortcut]\nURL='+pdf.resolve().as_uri()+'\n')
        # Only remove unchanged, previously generated indexes. Never delete originals
        # or files manually added to a project directory.
        for rel,expected in old.items():
            if rel in manifest:continue
            path=base/rel
            try:
                if not path.resolve().is_relative_to(base.resolve()):continue
                if path.is_file() and file_hash(path)==expected:path.unlink()
            except OSError:pass
        for rel in old:
            parent=(base/rel).parent
            while parent.is_relative_to(base) and parent!=base and parent!=directory('papers'):
                try:parent.rmdir()
                except OSError:break
                parent=parent.parent
        listing='<h1>文献罗盘 · 项目文件</h1><p>按软件中的分类查找论文、打开原文和阅读备注。索引可离线使用。</p><div class="actions">'
        for kind in ('originals','backups','exports','notes'):listing+='<a href="'+html.escape(directory(kind).resolve().as_uri())+'">'+LABELS[kind]+'</a>'
        listing+='</div><p class="muted">文献与分类目录由软件生成；在软件中调整后，点击“更新项目目录”。个人文件请放入“个人资料”，不会被目录更新移除。</p><input id="search" placeholder="按题名查找文献">'
        for name,items in sorted(groups.items()):
            listing+='<details open><summary>'+html.escape(name)+' · '+str(len(items))+' 篇</summary><ul>'
            for title,link in items:listing+='<li><a href="'+quote(link,safe='/')+'">'+html.escape(title)+'</a></li>'
            listing+='</ul></details>'
        listing+='<script>document.getElementById("search").oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll("li").forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(q));document.querySelectorAll("details").forEach(x=>x.hidden=![...x.querySelectorAll("li")].some(x=>!x.hidden));};</script>'
        emit(base/'项目索引.html',shell('文献罗盘项目索引',listing))
        emit(base/'目录说明.txt','从“项目索引.html”查找文献。\n文献目录：按文献库、阅读清单及多级分类生成的可读题名页面。\n原文索引：指向原始PDF的快捷入口，不移动原文件。\n备份：在软件中打包的工作空间ZIP，不会递归收入下一次备份。\n导出：从软件导出的CSV、RIS、GraphML或全文解读ZIP。\n个人资料：自行保存的文件；便携备份会包含这些文件，请勿放入不希望备份的敏感文件。\n分类和备注请在软件中修改，重新打开项目目录时更新索引。改动过的索引文件会先保存在“个人资料/保留的目录修改”。\n重新打开软件：双击桌面“文献罗盘”图标，或程序文件夹中的start.bat。\n')
        stats={'papers':len(papers),'reading':len(reading),'index_files':len(manifest),'preserved_edits':modified,'updated_at':datetime.now(timezone.utc).isoformat()}
        atomic_json(DATA_DIR/'private/project_files.manifest.json',{'files':manifest,'stats':stats})
        return stats

def locations():
    ensure_tree()
    return [{'kind':kind,'title':title,'path':str(directory(kind).resolve())} for kind,title in LABELS.items()]
