/* AI 速览面板（B 线）：唯一入口 window.WenxianAI.open(paperId, opts)。
   自管弹窗与样式；明确点击 AI 速览时可生成缺失结果，程序化打开默认只读。
   模型输出一律经 DOM textContent 插入，绝不作为 HTML。

   F1 修复（parallel_v02 B 线）：
   - 面板内部拆分为三个独立区域：来源区（原文摘要）、动作区（生成/重新生成按钮）、
     结果区（速览内容）。任一状态切换只更新自己的区域，绝不清空其他区域。
   - 论文信息取得后立即渲染动作区：新论文打开即有可点击的"生成速览"按钮。

   F2 修复（parallel_v02 B 线，方案：关闭详情再开 AI，关闭 AI 后返回详情）：
   - workspace.js 在调用 open() 前先关闭原生 <dialog> 详情弹窗（opts.returnTo），
     普通覆盖层不再与原生模态顶层冲突；AI 面板关闭时通过 showModal() 重新打开
     详情，并把焦点还原到详情中的"AI 速览"按钮（opts.returnFocus），焦点不丢失。

   竞态防护：
   - 每次打开面板递增 openSeq，所有异步回调持有并校验自己的序号；
   - 响应归属校验：只有 token 与 paperId 都仍然匹配当前面板时才写 DOM，
     快速切换论文 / 晚到响应不会把内容写进别的论文的面板；
   - 同一请求进行中（current.pending）按钮禁用，禁止重复发起；
   - 生成失败后错误信息保留、按钮恢复可用，可重试。

   F4 修复：说明文案按接口契约改为"实际发送题名与摘要，不发送 DOI / 内部 ID
   及其他库内字段"。 */
(function () {
  'use strict';
  var styleLoaded = false;
  var openSeq = 0; // 面板代际：每次打开递增，用于丢弃晚到的旧响应
  var current = {
    paperId: null, pending: false, abstract: null,
    lastStatus: 'not_generated', token: 0,
    returnTo: null, returnFocus: null
  };

  function ensureStyle() {
    if (styleLoaded) return;
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ai_brief.css?v=0.17.6';
    document.head.appendChild(link);
    styleLoaded = true;
  }

  function h(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  async function api(path, body) {
    var opts = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    };
    var resp = await fetch(path, opts);
    var data = null;
    try { data = await resp.json(); } catch (e) { /* 空/非 JSON 响应 */ }
    if (!resp.ok) {
      var detail = (data && (data.error && data.error.message || data.detail)) || ('HTTP ' + resp.status);
      var err = new Error(typeof detail === 'string' ? detail : '请求失败');
      err.payload = data;
      throw err;
    }
    return data;
  }

  var STATUS_TEXT = {
    ready: '已有 AI 速览', not_generated: '尚未生成速览',
    missing_source: '缺少摘要，无法生成', unavailable: '暂时不可用',
    failed: '生成失败'
  };
    var STALE_REASON_TEXT = {
    source_changed: '摘要已修改',
    title_changed: '题名已修改',
    prompt_version_changed: '提示词版本已更新',
    source_missing: '摘要已缺失',
    request_model_changed: '模型配置已更换',
    request_model_unverifiable: '当前模型配置不可读，无法比较'
  };
  function staleReasonText(data) {
    var reasons = (data && data.stale_reasons || []).map(function (r) {
      return STALE_REASON_TEXT[r] || r;
    });
    return reasons.length ? reasons.join('、') : '来源已变化';
  }
  var CODE_TEXT = {
    missing_source: '该论文缺少摘要。AI 速览仅依据论文现有摘要生成，不依据题名猜测，请先补全摘要。',
    offline_mode: '当前为离线模式：不产生新的外部请求。已有有效缓存会在下方显示。',
    not_configured: '模型服务配置不可用（未找到配置或密钥），无法生成。',
    auth_failed: '模型服务鉴权失败（401/403）。请检查本地配置中的密钥，已停止重试。',
    rate_limited: '模型服务限流（429），请稍后重试。',
    timeout: '模型服务请求超时，请稍后重试。',
    network_error: '无法连接模型服务网关，请检查网络后重试。',
    redirect_blocked: '模型服务网关返回重定向，已按安全策略阻止。',
    service_error: '模型服务服务错误，请稍后重试。',
    invalid_response: '模型服务返回了无法解析的响应。',
    empty_output: '模型服务返回了空内容。',
    invalid_model_output: '模型输出无法按固定结构解析或缺少必需字段，已拒绝显示。'
  };
  var ELEMENT_LABELS = {
    question: '研究问题', method: '研究方法',
    results: '研究结果', contribution: '贡献'
  };

  var els = {};
  var inFlight = new Map();
  function buildOverlay() {
    if (els.overlay) return;
    var overlay = h('div', 'wnai-overlay');
    overlay.hidden = true;
    var dialog = h('div', 'wnai-dialog');
    var header = h('div', 'wnai-header');
    var titleBox = h('div');
    titleBox.appendChild(h('h2', null, 'AI 速览'));
    header.appendChild(titleBox);
    var closeBtn = h('button', 'wnai-close', '×');
    closeBtn.setAttribute('aria-label', '关闭 AI 速览');
    var actions=h('div','wnai-header-actions');header.appendChild(actions);header.appendChild(closeBtn);
    var banner = h('div', 'wnai-banner', '依据摘要生成 · 可展开原文依据核对');
    var status = h('div', 'wnai-status');
    var errorBox = h('div', 'wnai-error');
    errorBox.hidden = true;
    // F1：三个独立区域，互不清空
    var sourceSec = h('details', 'wnai-section wnai-source');
    var genSec = h('div', 'wnai-section wnai-gen');
    var resultSec = h('div', 'wnai-section');
    dialog.appendChild(header); dialog.appendChild(banner);
    dialog.appendChild(status); dialog.appendChild(errorBox);
    var body=h('div','wnai-body');body.appendChild(genSec);body.appendChild(resultSec);body.appendChild(sourceSec);dialog.appendChild(body);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    closeBtn.addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && els.overlay && !els.overlay.hidden) { e.stopPropagation(); close(); }
    }, true);
    els = {
      overlay: overlay, status: status, errorBox: errorBox,
      sourceSec: sourceSec, genSec: genSec, resultSec: resultSec, actions:actions
    };
  }

  function close() {
    if (els.overlay) els.overlay.hidden = true;
    current.pending = false;
    // F2：返回详情流程——重新打开详情面板并还原焦点到 AI 入口按钮。
    // A 线 v06：详情默认为非模态侧边面板，优先 .show()；仅 showModal 可用时（旧环境/测试桩）回退。
    var returnTo = current.returnTo, returnFocus = current.returnFocus;
    current.returnTo = null; current.returnFocus = null;
    if (returnTo && returnTo.isConnected && !returnTo.open) {
      if (typeof returnTo.show === 'function') returnTo.show();
      else if (typeof returnTo.showModal === 'function') returnTo.showModal();
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    }
  }

  function setStatus(nodes) {
    els.status.textContent = '';
    nodes.forEach(function (n) { els.status.appendChild(n); });
  }

  function showError(code, fallback) {
    // 无 code 且无 fallback：隐藏；仅有 fallback（如浏览器级网络失败）时显示 fallback
    if (!code && !fallback) { els.errorBox.hidden = true; return; }
    els.errorBox.textContent = CODE_TEXT[code] || fallback || '生成未成功。';
    els.errorBox.hidden = false;
  }

  // F1：只清结果区，绝不动来源区与动作区
  function clearResult() {
    els.resultSec.textContent = '';
  }

  function updateGenBtn() {
    var btn = current.genBtn;
    if (!btn || current.pending) return;
    btn.disabled = !current.abstract?.trim();
    btn.textContent = current.lastStatus === 'ready' ? '重新生成' : current.lastStatus==='not_generated'?'生成速览':'重试';
  }

  function renderSource(paper) {
    var sec = els.sourceSec;
    sec.textContent = '';
    sec.appendChild(h('summary', null, '查看原始题名与摘要'));sec.open=false;
    // C 线 parallel_v03：原文题名 / 中文题名 / 原文摘要 / 中文摘要 / 已有总结分区展示，
    // 中文内容的复用来源标注（§3.1）由 existing 端点或 brief.reuse 提供时显示。
    var fieldTitle = h('div', 'wnai-field');
    fieldTitle.appendChild(h('span', 'wnai-field-label', '原文题名'));
    fieldTitle.appendChild(h('p', 'wnai-paper-title', paper.title || paper.title_zh || '未命名文献'));
    sec.appendChild(fieldTitle);
    if (paper.title_zh && paper.title_zh !== paper.title) {
      var fieldTitleZh = h('div', 'wnai-field');
      fieldTitleZh.id = 'wnai-field-title-zh';
      fieldTitleZh.appendChild(h('span', 'wnai-field-label', '中文题名'));
      fieldTitleZh.appendChild(h('p', 'wnai-paper-title wnai-title-zh', paper.title_zh));
      sec.appendChild(fieldTitleZh);
    }
    var fieldAbs = h('div', 'wnai-field');
    fieldAbs.appendChild(h('span', 'wnai-field-label', '原文摘要'));
    var absText = paper.abstract || '';
    current.abstract = absText;
    var box = h('div', 'wnai-abstract');
    box.id = 'wnai-source-abstract';
    box.textContent = absText || '本地暂无摘要。';
    fieldAbs.appendChild(box);
    sec.appendChild(fieldAbs);
    if (paper.abstract_zh && paper.abstract_zh !== paper.abstract) {
      var fieldAbsZh = h('div', 'wnai-field');
      fieldAbsZh.id = 'wnai-field-abstract-zh';
      fieldAbsZh.appendChild(h('span', 'wnai-field-label', '已有中文摘要'));
      var boxZh = h('div', 'wnai-abstract wnai-abstract-zh');
      boxZh.textContent = paper.abstract_zh;
      fieldAbsZh.appendChild(boxZh);
      sec.appendChild(fieldAbsZh);
    }
    if (!absText) sec.appendChild(h('p', 'wnai-muted', '缺少摘要：AI 速览仅依据摘要生成，不会依据题名补造内容。'));
    attachExistingToSource(paper.id);
  }

  var FALLBACK_ORIGIN_TEXT = {
    library_human: '人工内容',
    library_unknown_source: '已有内容，来源信息未记录',
    ai_with_source: 'AI 生成（有来源）',
    zotero: '来自 Zotero'
  };
  function originText(origin) {
    var X = window.WenxianExisting;
    return (X && X.originLabel(origin)) || FALLBACK_ORIGIN_TEXT[origin] || null;
  }
  function originBadgeEl(origin) {
    var text = originText(origin);
    return text ? h('span', 'wnai-origin', text) : null;
  }

  // 已有内容（§3.1）：GET /api/papers/{id}/existing（B 线 seam，尚未合入时优雅降级隐藏）
  async function attachExistingToSource(paperId) {
    try {
      var resp = await fetch('/api/papers/' + paperId + '/existing');
      if (!resp.ok) return;
      var data = await resp.json();
      if (current.paperId !== paperId) return; // 面板已切到别的论文
      if (!data || typeof data !== 'object') return;
      var titleZh = document.getElementById('wnai-field-title-zh');
      if (titleZh && data.title_zh && data.title_zh.text != null) {
        var b1 = originBadgeEl(data.title_zh.origin);
        if (b1) titleZh.insertBefore(b1, titleZh.firstChild);
      }
      var absZh = document.getElementById('wnai-field-abstract-zh');
      if (absZh && data.abstract_zh && data.abstract_zh.text != null) {
        var b2 = originBadgeEl(data.abstract_zh.origin);
        if (b2) absZh.insertBefore(b2, absZh.firstChild);
      }
      var sum = data.summary;
      if (sum && sum.text) {
        var secSum = h('div', 'wnai-field wnai-existing');
        secSum.appendChild(h('span', 'wnai-field-label', '已有总结（库内已有内容，非本次生成）'));
        var b3 = originBadgeEl(sum.origin);
        if (b3) secSum.appendChild(b3);
        var boxSum = h('div', 'wnai-abstract wnai-abstract-zh');
        boxSum.textContent = sum.text;
        secSum.appendChild(boxSum);
        els.sourceSec.appendChild(secSum);
      }
    } catch (e) { /* 端点不存在/网络失败：已有总结区块保持隐藏，不报错 */ }
  }

  function renderGenerate() {
    els.genSec.textContent='';els.genSec.hidden=true;els.actions.textContent='';
    var btn=h('button','wnai-regenerate','生成速览');btn.id='wnai-generate-btn';btn.type='button';
    btn.setAttribute('aria-label','重新生成或重试本篇 AI 速览');els.actions.appendChild(btn);current.genBtn=btn;
    btn.addEventListener('click',async function(){
      if(current.pending)return;
      var myToken=current.token,pid=current.paperId,force=current.lastStatus==='ready';
      current.pending=true;btn.disabled=true;btn.textContent='正在生成…';els.errorBox.hidden=true;
      setStatus([h('span',null,'正在生成速览…')]);
      async function restoreOldResult(){try{var old=await api('/api/ai/briefs/'+pid);if(myToken===current.token&&old.status==='ready')applyState(old,{keepError:true});}catch(e){}}
      try{
        var request=inFlight.get(pid);
        if(!request){request=api('/api/ai/briefs',{paper_id:pid,source_kind:'abstract',force:force});inFlight.set(pid,request);
          (function(value,id){value.then(function(){if(inFlight.get(id)===value)inFlight.delete(id);},function(){if(inFlight.get(id)===value)inFlight.delete(id);});})(request,pid);}
        var data=await request;if(myToken!==current.token)return;applyState(data);if(data.status!=='ready')await restoreOldResult();
      }catch(err){
        if(myToken!==current.token)return;showError(err.payload&&err.payload.error&&err.payload.error.code,err.message);
        setStatus([h('span',null,'生成未完成，可重试；已有结果保留。')]);await restoreOldResult();
      }finally{if(myToken===current.token){current.pending=false;updateGenBtn();}}
    });
  }

  // 复用来源标注（§3.1）：A 线在 brief 响应里提供 reuse 字段（形状宽容解析）：
  //   brief.reuse = { title_zh: {origin:"library_human"} | "library_human", abstract_zh: {...}, ... }
  // 也兼容顶层数据 data.reuse。字段无标注时不显示标签（视为本次新生成内容）。
  function reuseOriginFor(brief, data, field) {
    var r = (brief && brief.reuse) || (data && data.reuse) || null;
    if (!r || typeof r !== 'object') return null;
    var v = r[field];
    if (v && typeof v === 'object') v = v.origin;
    return typeof v === 'string' ? v : null;
  }

  // V4-08 修复：内容状态说明依据“真正保存的字段产物”渲染（契约 3.6/3.7）。
  // reuse.fields.<field> = {reused, origin, kind, kind_label, label, text, display_source}（形状宽容解析）。
  // reused=true → 显示已有来源与五分类状态；未保存的总结不再照抄后端旧版
  // 缺失类注记，改写为与四要素生成状态一致的中性说明。
  function reuseFieldsMap(data) {
    var r = (data && (data.reuse && (data.reuse.fields || data.reuse))) || null;
    return r && typeof r === 'object' ? r : null;
  }

  function renderContentStatus(data, brief) {
    var X = window.WenxianExisting;
    var fields = reuseFieldsMap(data);
    var sec = h('details', 'wnai-section wnai-content-status');
    sec.appendChild(h('summary', null, '内容来源'));
    var hasAny = false;
    if (fields && typeof fields === 'object') {
      Object.keys(fields).forEach(function (name) {
        var entry = fields[name];
        if (!entry || typeof entry !== 'object') return;
        hasAny = true;
        var labelName = (X && X.fieldLabel ? X.fieldLabel(name) : name);
        var category = X && X.contentStatusCategory ? X.contentStatusCategory(entry) : 'existing';
        var text = X && X.contentStatusText ? X.contentStatusText(category, name) : null;
        var rowEl = h('div', 'wnai-kv');
        rowEl.appendChild(h('span', 'wnai-kind', labelName));
        var val = h('span');
        val.textContent = text || (entry.reused ? '已有内容（优先复用）' : '本篇暂无已保存的该字段产物');
        rowEl.appendChild(val);
        var badge = originBadgeEl(entry.origin);
        if (badge) rowEl.appendChild(badge);
        sec.appendChild(rowEl);
      });
    }
    // 总结注记：与四要素生成状态保持一致，绝不出现“AI 已生成四要素却提示总结仍缺失”的自相矛盾。
    var summaryEntry = fields && fields.summary && typeof fields.summary === 'object' ? fields.summary : null;
    var note = h('p', 'wnai-muted');
    if (summaryEntry && summaryEntry.reused && summaryEntry.text) {
      note.textContent = '已有总结已复用（见来源区“已有总结”），本次未重新生成总结。';
    } else if (brief && brief.elements) {
      note.textContent = '本次结果为双语概括与四要素（各附原文引用）；总结类产物以库内已保存内容为准，未保存时不在此显示总结正文。';
    } else {
      note.textContent = '总结类产物以库内已保存内容为准；未保存时不显示总结正文。';
    }
    sec.appendChild(note);
    els.resultSec.appendChild(sec);
    return hasAny;
  }

  function renderBrief(brief, cached, stale, data) {
    var sec = h('div', 'wnai-section');
    sec.appendChild(h('h3', null, '研究概览'));
    var biling = h('div', 'wnai-biling');
    function kv(kind, text, reuseField) {
      var rowEl = h('div', 'wnai-kv');
      rowEl.appendChild(h('span', 'wnai-kind', kind));
      var val = h('span');
      val.textContent = text;
      rowEl.appendChild(val);
      if (reuseField) {
        var badge = originBadgeEl(reuseOriginFor(brief, data, reuseField));
        if (badge) rowEl.appendChild(badge); // 复用来源：人工内容 / 已有内容（来源未记录）/ AI 生成（有来源）
      }
      return rowEl;
    }
    biling.appendChild(kv('题名 · 翻译', brief.title_zh, 'title_zh'));
    biling.appendChild(kv('摘要 · 中文概括', brief.abstract_zh, 'abstract_zh'));
    var english=h('details','wnai-english');english.appendChild(h('summary',null,'英文概括'));english.appendChild(kv('English',brief.abstract_en,'abstract_en'));biling.appendChild(english);
    sec.appendChild(biling);
    els.resultSec.appendChild(sec);

    var sec2 = h('div', 'wnai-section');
    sec2.appendChild(h('h3', null, '研究要点'));
    Object.keys(ELEMENT_LABELS).forEach(function (name) {
      var item = brief.elements && brief.elements[name];
      var box = h('div', 'wnai-elem');
      box.appendChild(h('h4', null, ELEMENT_LABELS[name]));
      if (!item) { box.appendChild(h('p', 'wnai-muted', '摘要未提供')); sec2.appendChild(box); return; }
      var zh = h('p');
      zh.textContent = item.zh || '摘要未提供';
      box.appendChild(zh);
      var en=h('details','wnai-english');en.appendChild(h('summary',null,'英文表述'));en.appendChild(h('p','wnai-en',item.en||''));box.appendChild(en);
      if (item.quote_verified && item.quote) {
        var q = h('details', 'wnai-quote');q.appendChild(h('summary',null,'原文依据'));
        q.appendChild(document.createTextNode('原文引用：' + item.quote));
        var loc = h('button', null, '定位');
        loc.setAttribute('aria-label', '在上方原始摘要中定位该引用');
        loc.addEventListener('click', function () { highlightQuote(item.quote, brief.source_snapshot_raw); });
        q.appendChild(loc);
        if (typeof item.quote_start === 'number')
          loc.title='摘要位置 '+item.quote_start+'–'+item.quote_end;
        box.appendChild(q);
      } else if (item.quote) {
        box.appendChild(h('div', 'wnai-noquote',
          '模型给出的引用未能在本次输入摘要中逐字找到，不作为已验证证据显示。'));
      } else {
        box.appendChild(h('div', 'wnai-noquote', '摘要未提供该要素的原文依据。'));
      }
      sec2.appendChild(box);
    });
    els.resultSec.appendChild(sec2);

    if (brief.warnings && brief.warnings.length) {
      els.resultSec.appendChild(h('div', 'wnai-warning', '校验提示：' + brief.warnings.join('；')));
    }
    var meta = h('details', 'wnai-meta');
    meta.textContent = '请求模型：' + (brief.request_model || '未知') +
      ' · 响应模型：' + (brief.response_model || '未知') +
      ' · prompt 版本：' + (brief.prompt_version || '未知') +
      ' · 生成时间：' + (brief.generated_at || '未知') +
      ' · 来源：' + (brief.source_kind || 'abstract') +
      (cached ? ' · 缓存命中' : ' · 本次新生成') +
      (stale ? ' · 结果已过期（对应旧来源快照）' : '') +
      ' · 输入哈希：' + (brief.source_hash || '未知').slice(0, 16) + '…';
    var metadata=h('p',null,meta.textContent);meta.textContent='';meta.appendChild(h('summary',null,'生成信息'));meta.appendChild(metadata);els.resultSec.appendChild(meta);
    // V4-08：内容状态说明（五分类）+ 与生成状态一致的总结注记
    renderContentStatus(data, brief);
  }

  // A 契约：quote 对应生成时的原始摘要快照（brief.source_snapshot_raw）。
  // 优先在当前摘要中定位；找不到时回退显示生成时快照再定位（含原始空白）。
  function highlightQuote(quote, snapshotRaw) {
    var box = document.getElementById('wnai-source-abstract');
    if (!box) return;els.sourceSec.open=true;
    var basis = current.abstract;
    var usingSnapshot = false;
    if ((!basis || basis.indexOf(quote) < 0) &&
        typeof snapshotRaw === 'string' && snapshotRaw.indexOf(quote) >= 0) {
      basis = snapshotRaw;
      usingSnapshot = true;
    }
    if (!basis || basis.indexOf(quote) < 0) return;
    var idx = basis.indexOf(quote);
    var prefix = basis.slice(0, idx);
    var rest = basis.slice(idx + quote.length);
    if (usingSnapshot) {
      box.textContent = '';
      box.appendChild(h('div', 'wnai-muted',
        '以下为生成本次结果时的原始摘要快照（当前摘要中已找不到该引用）：'));
    } else {
      box.textContent = '';
    }
    box.appendChild(document.createTextNode(prefix));
    var mark = h('mark', 'wnai-mark', quote);
    mark.id = 'wnai-mark-target';
    box.appendChild(mark);
    if (rest) box.appendChild(document.createTextNode(rest));
    var t = document.getElementById('wnai-mark-target');
    if (t) t.scrollIntoView({ block: 'center' });
  }

  // F1：applyState 只更新状态行与结果区，不再清空来源区 / 动作区
  // opts.keepError：恢复旧结果展示时保留已有错误信息
  function applyState(data, opts) {
    current.lastStatus = data.status || current.lastStatus;
    var nodes = [h('span', null, STATUS_TEXT[data.status] || data.status)];
    if (data.status === 'ready') {
      nodes.push(h('span', 'wnai-badge ' + (data.cached ? 'cached' : 'fresh'),
        data.cached ? '缓存结果' : '本次新生成'));
      if (data.stale) nodes.push(h('span', 'wnai-badge stale',
        staleReasonText(data) + '，此结果对应旧来源（已过期）'));
      else if (data.validity === 'unverifiable') nodes.push(h('span', 'wnai-badge stale',
        '当前模型配置不可读，无法比较；以下为历史结果，未验证有效'));
    }
    setStatus(nodes);
    clearResult();
    if (data.status === 'ready' && data.brief) {
      if (!(opts && opts.keepError)) showError(null);
      renderBrief(data.brief, data.cached, data.stale, data);
    } else if (data.error && data.error.code && data.error.code !== 'ok') {
      showError(data.error.code, data.error.message);
      els.resultSec.appendChild(h('p', 'wnai-muted',
        '上方错误信息会保留；修正环境或网络后，可点击上方按钮重试。已有摘要与可用结果不受影响。'));
    } else if (data.status === 'not_generated') {
      showError(null);
      els.resultSec.appendChild(h('p', 'wnai-muted',
        '尚无 AI 速览结果。'));
    } else if (data.status === 'missing_source') {
      els.resultSec.appendChild(h('p', 'wnai-muted',
        '缺少摘要：请先在库内补全本篇论文的摘要元数据，再生成速览。'));
    } else if (data.status === 'unavailable' || data.status === 'failed') {
      els.resultSec.appendChild(h('p', 'wnai-muted',
        '本次未能生成结果；上方错误信息会保留，可重试。'));
    }
    updateGenBtn();
  }

  async function openPanel(paperId, opts) {
    ensureStyle();
    buildOverlay();
    var pid = Number(paperId);
    var token = ++openSeq;
    current.token = token;
    current.paperId = pid;
    current.abstract = null;
    current.pending = false;
    current.lastStatus = 'not_generated';
    current.genBtn = null;
    current.returnTo = (opts && opts.returnTo) || null;
    current.returnFocus = (opts && opts.returnFocus) || null;
    els.errorBox.hidden = true;
    els.sourceSec.textContent = '';
    els.genSec.textContent = '';
    els.actions.textContent='';
    els.resultSec.textContent = '';
    setStatus([h('span', null, '正在读取论文信息…')]);
    els.resultSec.appendChild(h('p', 'wnai-muted', '正在读取 AI 速览状态…'));
    els.overlay.hidden = false;
    var focusTarget = els.overlay.querySelector('.wnai-close');
    if (focusTarget) focusTarget.focus(); // 焦点进入 AI 面板，不留在被关闭的详情里
    try {
      // 1) 先读论文信息，动作按钮在缓存状态确认后启用。
      var paper = await api('/api/papers/' + pid);
      if (token !== current.token || current.paperId !== pid) return;
      setStatus([h('span', null, '正在读取 AI 速览状态…')]);
      renderSource(paper);

      // 2) 已有结果状态（只读，不触发生成）
      var data = await api('/api/ai/briefs/' + pid);
      if (token !== current.token || current.paperId !== pid) return;
      renderGenerate();applyState(data);
      if(opts&&opts.generateIfMissing&&data.status==='not_generated'&&paper.abstract&&paper.abstract.trim()){
        var gate=await Promise.all([api('/api/ai/config'),api('/api/settings')]);
        if(token!==current.token||els.overlay.hidden)return;
        if(current.lastStatus!=='not_generated'||current.pending)return;
        if(gate[0].configured&&gate[1].offline_mode!=='1'){current.genBtn.click();}
        else{showError(gate[1].offline_mode==='1'?'offline_mode':'not_configured');}
      }
    } catch (err) {
      if (token !== current.token || current.paperId !== pid) return;
      clearResult();
      els.resultSec.appendChild(h('p', 'wnai-muted', '读取失败：' + err.message));
      if (current.genBtn) updateGenBtn();
    }
  }

  window.WenxianAI = { open: openPanel };
})();
