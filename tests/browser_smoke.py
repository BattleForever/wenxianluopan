"""Optional browser checks against a running local application.

Install Playwright and its Chromium separately, then run this script with
--base http://127.0.0.1:8765. Model responses are intercepted fixtures; no model
calls, stored configuration changes or Zotero writes occur in these checks.
"""
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright


async def run(base,out):
    out.mkdir(parents=True,exist_ok=True)
    checks=[]
    errors=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        context=await browser.new_context(viewport={'width':1440,'height':900},locale='zh-CN')
        page=await context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.goto(base,wait_until='networkidle')
        await page.locator('[data-page="settings"]').click()
        await page.wait_for_function("document.getElementById('ai-config-state').textContent !== '尚未读取配置'")
        assert await page.locator('#ai-key-input').input_value()==''
        checks.append('saved credential never echoed to password field')
        await page.locator('#ai-service').select_option('deepseek')
        assert not await page.locator('#ai-url-row').is_visible()
        await page.locator('#ai-service').select_option('custom')
        assert await page.locator('#ai-url-row').is_visible()
        checks.append('provider-specific address visibility')
        await page.screenshot(path=str(out/'settings.png'))
        e={'page':1,'quote':'This is an exact test source.'}
        guide={'overview':'<img src=x onerror=alert(1)>','institutions':'Test institute',
               'sections':[{'heading':'Introduction','explanation':'Test explanation','evidence':e}],
               'figures':[],'conclusion':{'text':'Test conclusion','evidence':e}}
        seen=[]
        async def intercept(route):
            pid=int(route.request.url.split('/papers/')[1].split('/')[0]);seen.append(pid)
            await asyncio.sleep(.4 if pid==1 else .02)
            payload={'status':'ready','guide':{**guide,'title_zh':'FIRST' if pid==1 else 'SECOND'},
                     'source_title':'Test source','model':'fixture','pages':1,'created_at':'2026-01-01',
                     'source_hash':'a'*64,'stale':False,'stale_reasons':[],'validity':'unverifiable'}
            try:await route.fulfill(json=payload)
            except Exception:pass  # an aborted older request is expected
        await page.route('**/api/papers/*/pdf-guide',intercept)
        await page.evaluate("() => { window.WenxianPDF.open(1); setTimeout(()=>window.WenxianPDF.open(2),25); }")
        await page.locator('.pg-header h2').filter(has_text='SECOND').wait_for()
        await asyncio.sleep(.5)
        assert await page.locator('.pg-header h2').inner_text()=='SECOND'
        assert await page.locator('.pg-main img').count()==0
        assert '<img src=x' in await page.locator('.pg-main').inner_text()
        checks.extend(['rapid paper switch ignores older response','model HTML is displayed as inert text'])
        await page.get_by_role('button',name='逐节导读',exact=True).click()
        assert await page.locator('.pg-evidence').count()==1
        checks.append('section navigation and evidence controls')
        await page.locator('[data-pg-close]').click()
        await page.evaluate('() => { window.WenxianPDF.open(1); setTimeout(()=>document.querySelector("#pdf-guide-dialog").close(),30); }')
        await asyncio.sleep(.7)
        assert not await page.locator('#pdf-guide-dialog').is_visible()
        total=len(seen);await asyncio.sleep(2.2);assert len(seen)==total
        checks.append('closing cancels response rendering and polling')
        await page.unroute('**/api/papers/*/pdf-guide',intercept)
        assert not errors,errors
        await browser.close()
    report={'checks':checks,'passed':len(checks),'page_errors':errors}
    (out/'browser_checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:8765');p.add_argument('--out',type=Path,default=Path('output/playwright'))
    args=p.parse_args();asyncio.run(run(args.base,args.out))
