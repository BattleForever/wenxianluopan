"""Optional scholarly service credentials; public status only."""
from fastapi import APIRouter,HTTPException
from .config import DATA_DIR
from .private_settings import LOCK,read_json,atomic_json,service_key
import json,os,re,tempfile
router=APIRouter(prefix='/api/data-sources')
NAMES={'openalex','easyscholar'}

@router.get('')
def status():
    from .venues import load_key
    return {'openalex':{'configured':bool(service_key('openalex'))},'easyscholar':{'configured':bool(load_key())}}

@router.post('')
def save(body:dict):
    name=body.get('service')
    if not isinstance(name,str) or name not in NAMES:raise HTTPException(400,'不支持的数据源')
    key=body.get('api_key','')
    if not isinstance(key,str) or len(key)>8192 or any(ord(c)<32 or ord(c)==127 for c in key):raise HTTPException(400,'密钥不能包含换行或控制字符')
    if not key.strip() and body.get('clear') is not True:raise HTTPException(400,'请填写密钥，或选择删除已保存的配置')
    with LOCK:
        path=DATA_DIR/'private'/'data_sources.local.json';value=read_json(path)
        value[name]=None if body.get('clear') is True else key.strip()
        try:atomic_json(path,value)
        except OSError:raise HTTPException(500,'数据源设置保存失败')
    return {'saved':True,**status()}

@router.post('/metrics-cache')
def import_metrics_cache(body:dict):
    from . import venues
    from .linkview import number,zone_number
    text=body.get('text')
    if not isinstance(text,str) or len(text.encode('utf-8'))>8_000_000:raise HTTPException(400,'请选择不超过 8 MB 的期刊指标 JSONL 文件')
    lines=[line for line in text.splitlines() if line.strip()]
    if len(lines)>10000:raise HTTPException(400,'每次最多导入 10000 条期刊指标')
    records={};rejected=0
    for line in lines:
        try:
            row=json.loads(line);name=row.get('name');rank=row.get('rank')
            if not isinstance(name,str) or not name.strip() or len(name)>300 or not isinstance(rank,dict):raise ValueError()
            clean={}
            for key in ('sciif','sci','sciUp'):
                value=rank.get(key)
                if not isinstance(value,(str,int,float)) or isinstance(value,bool):continue
                value=str(value).strip()
                if len(value)>40:continue
                valid=number(value) if key=='sciif' else zone_number(value)
                if valid is not None and (key!='sciif' or 0<=valid<=1000):
                    if key=='sciif':
                        if re.fullmatch(r'\d+(?:\.\d+)?',value):clean[key]=value
                    else:clean[key]='Q'+str(valid) if key=='sci' else str(valid)+'区'
            if not clean:raise ValueError()
            rec={'name':name.strip(),'k':venues.norm_name(name),'rank':clean,'imported':True}
            at=str(row.get('at') or '')
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?',at):rec['at']=at
            year=row.get('metric_year')
            if isinstance(year,int) and not isinstance(year,bool) and 1900<=year<=2099:rec['metric_year']=year
            records[rec['k']]=rec
        except (ValueError,TypeError,AttributeError):rejected+=1
    if not records:raise HTTPException(400,'文件中没有有效的 easyScholar 期刊指标记录')
    with LOCK:
        previous=venues._load_cache()
        venues.remember_records([*previous.values(),*records.values()])
        values={**previous,**records};path=venues.CACHE_PATH;path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(dir=path.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as out:
                for row in values.values():out.write(json.dumps(row,ensure_ascii=False)+'\n')
            os.replace(name,path);venues._cache=values
        except OSError:raise HTTPException(500,'期刊指标缓存保存失败')
        finally:
            from pathlib import Path
            Path(name).unlink(missing_ok=True)
    from .metric_jobs import hydrate_cached
    fields=hydrate_cached()
    return {'imported':len(records),'rejected':rejected,'fields_filled':fields,'message':'已批量匹配文献中的期刊指标。缓存日期不作为指标年份。'}

@router.get('/metrics-catalog')
def metrics_catalog():
    from .venues import catalog_stats
    return {**catalog_stats(),'storage':'local','message':'期刊指标保存在本机；按期刊复用，不按论文重复查询。'}
