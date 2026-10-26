"""Credential-free, bounded views and portable exports of the local metric cache."""
import csv,io,json,os,subprocess,sys
from fastapi import APIRouter,HTTPException,Request,Query
from fastapi.responses import FileResponse
from . import venues,project_files
from .private_settings import LOCK
router=APIRouter(prefix='/api/data-sources/metrics-catalog')

def rows():
    with LOCK:values=list(venues._load_cache().values())
    result=[]
    for row in values:
        if not isinstance(row.get('name'),str) or not isinstance(row.get('rank'),dict):continue
        rank={k:str(row['rank'][k]) for k in ('sciif','sci','sciUp') if row['rank'].get(k) not in (None,'','null')}
        if not rank:continue
        result.append({'name':row['name'],'rank':rank,'metric_year':row.get('metric_year'),'at':str(row.get('at') or ''),'source':'导入缓存' if row.get('imported') else 'easyScholar'})
    return sorted(result,key=lambda r:venues.norm_name(r['name']))

@router.get('/items')
def items(q:str=Query('',max_length=300),offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200)):
    all_rows=rows();key=venues.norm_name(q);filtered=[r for r in all_rows if not key or key in venues.norm_name(r['name'])]
    return {'items':filtered[offset:offset+limit],'total':len(filtered),'catalog_total':len(all_rows),'offset':offset,'limit':limit}

def snapshot(format):
    data=rows()
    if format=='jsonl':text=''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in data);extension='jsonl'
    elif format=='csv':
        stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['name','impact_factor','cas_zone','jcr_zone','metric_year','checked_at','source'])
        safe=lambda value:"'"+str(value) if str(value).startswith(('=','+','-','@','\t','\r')) else value
        for r in data:writer.writerow([safe(v) for v in [r['name'],r['rank'].get('sciif',''),r['rank'].get('sciUp',''),r['rank'].get('sci',''),r.get('metric_year') or '',r['at'],r['source']]])
        text='\ufeff'+stream.getvalue();extension='csv'
    else:raise HTTPException(422,'请选择 CSV 或 JSONL')
    return project_files.save_export('期刊指标库',extension,text)

@router.get('/export')
def export(format:str='csv'):
    path=snapshot(format)
    return FileResponse(path,filename=path.name,media_type='text/csv; charset=utf-8' if format=='csv' else 'application/x-ndjson')

@router.post('/open-file')
def open_file(request:Request):
    from .workspace_data import authorize
    authorize(request);path=snapshot('csv')
    try:
        if os.name=='nt':os.startfile(str(path))
        else:subprocess.Popen(['open' if sys.platform=='darwin' else 'xdg-open',str(path)])
    except OSError:raise HTTPException(409,'表格已导出；当前电脑没有关联的表格程序，可用导出按钮下载后打开')
    return {'opened':True,'filename':path.name,'message':'已打开本地 CSV 查看副本；修改后通过导入更新指标库'}

def import_text(text):
    """Normalize common tables; the existing importer still validates every field."""
    stripped=text.lstrip('\ufeff \r\n\t')
    def normalize(row):
        if not isinstance(row,dict):return row
        if isinstance(row.get('rank'),dict):return row
        aliases={'name':('name','期刊名称','期刊','Journal'),'sciif':('impact_factor','影响因子','IF'),'sciUp':('cas_zone','中科院分区'),'sci':('jcr_zone','JCR分区')}
        out={'name':next((row[k] for k in aliases['name'] if row.get(k)),None),'rank':{}}
        for key in ('sciif','sciUp','sci'):
            out['rank'][key]=next((row[k] for k in aliases[key] if row.get(k) not in (None,'')),None)
        year=row.get('metric_year') or row.get('指标年份');out['at']=row.get('checked_at') or row.get('at') or ''
        if year not in (None,''):
            try:out['metric_year']=int(year)
            except (ValueError,TypeError):pass
        return out
    if stripped.startswith('['):
        try:data=json.loads(stripped)
        except ValueError:raise HTTPException(400,'JSON 文件格式不正确')
    elif stripped.startswith('{'):
        data=[]
        for line in stripped.splitlines():
            if not line.strip():continue
            try:data.append(json.loads(line))
            except ValueError:data.append(None)
    else:
        try:data=list(csv.DictReader(io.StringIO(stripped)))
        except csv.Error:raise HTTPException(400,'CSV 文件格式不正确')
    if len(data)>10000:raise HTTPException(400,'每次最多导入10000个期刊')
    return '\n'.join(json.dumps(normalize(row),ensure_ascii=False) for row in data)
