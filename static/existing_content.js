/* 文献罗盘 · C 线（parallel_v04）公共纯函数模块：复用来源标注、Zotero 同步状态、
 * 字段补全结果与内容状态说明。
 *
 * 接口契约（接口与分工.md §3.1/§3.3/§3.6/§3.7，v04 定版）：
 * §3.1 复用来源 origin 语义（A 线在 /api/ai/briefs* 响应里给 reuse 标注字段；
 *      B 线 get_existing_content 返回同语义 origin）：
 *   - library_human           → 人工内容
 *   - library_unknown_source  → 已有内容，来源信息未记录（不得伪造模型/时间）
 *   - ai_with_source          → AI 生成（有来源）（仅当能对上来源哈希）
 *   - zotero                  → 来自 Zotero 库
 *   - null / 缺失             → 无标注（界面上不显示标签；AI 新生成内容由调用方自行标注）
 * §3.3 同步状态（GET /api/library/sync/status?paper_id=N）：
 *   每条记录 { local_saved: bool, zotero_state: "none"|"pending"|"partial"|"synced"|"conflict", last_error: str|None }
 *   展示文案：none→已保存本地；pending→等待 Zotero；partial→部分完成（剩余待办）；
 *   synced→两端已同步；conflict→冲突。旧四态文案保持不变，仅新增 partial。
 * §3.1(v04) 字段补全 POST /api/enrich/missing 的 errors.code 五类 Zotero 错误
 *   （zotero_not_enabled / zotero_unreachable / zotero_not_found /
 *     zotero_auth_missing / zotero_config_invalid）分别给出可读文案，
 *   不得合并成一种；另有网络/AI 相关错误码。
 *   端点不存在（404/网络错误）时调用方必须优雅降级隐藏区块，本模块只提供纯函数。
 *
 * 本文件不含任何 DOM/网络调用，可在 Node 中直接加载自测。
 */
(function (root) {
  'use strict';

  var ORIGIN_TEXT = {
    library_human: '人工内容',
    library_unknown_source: '已有内容，来源信息未记录',
    ai_with_source: 'AI 生成（有来源）',
    zotero: '来自 Zotero',
    network_biblio: '有来源网络书目'
  };

  var SYNC_STATE_TEXT = {
    none: '已保存本地',
    pending: '等待 Zotero',
    partial: '部分完成（剩余待办）',
    synced: '两端已同步',
    conflict: '冲突'
  };

  /* §3.1(v04)：POST /api/enrich/missing 的错误码 → 可读文案。
   * 五类 Zotero 错误必须分别显示，不得吞并成一种。 */
  var ZOTERO_ERROR_TEXT = {
    zotero_not_enabled: 'Zotero 集成未启用：请先在“数据与设置”中启用 Zotero 集成，并确认 Zotero 正在运行。',
    zotero_unreachable: 'Zotero 不可达：本机 127.0.0.1:23119 未响应，请确认 Zotero 已打开且本地 API 可用。',
    zotero_not_found: 'Zotero 中没有此条目：本地 Zotero 库未找到与该文献匹配的条目，可先在 Zotero 中添加后再同步。',
    zotero_auth_missing: '缺少 Zotero 写回授权：本地 API 授权尚未完成（需携带 Zotero-Server-ID 校验），请完成本地授权后重试。',
    zotero_config_invalid: 'Zotero 配置错误：本地 API 地址或响应不符合约定，请检查配置后重试。',
    network_disabled: '联网已关闭（离线模式）：本次未执行网络补全，可开启联网后重试。',
    network_failed: '联网补全失败：学术数据源暂不可用或请求受限，可稍后重试。',
    no_source_for_ai: '没有可依据的原文：AI 补全需要原文摘要等依据，本次未执行。',
    ai_disabled: 'AI 补全未启用：本次仅允许本地与网络来源。'
  };

  /* §3.1(v04) 字段集 → 界面标签 */
  var FIELD_LABELS = {
    title: '原文题名', title_zh: '中文题名',
    abstract: '原文摘要', abstract_zh: '中文摘要',
    summary: '总结',
    question: '研究问题', method: '研究方法',
    results: '研究结果', contribution: '贡献'
  };

  function originLabel(origin) {
    if (origin == null || origin === '') return null; // 无标注 → 不显示
    return ORIGIN_TEXT[origin] || null;               // 未知值不臆造文案
  }

  function syncStateText(state) {
    return SYNC_STATE_TEXT[state] || null; // 未知状态返回 null，由调用方决定降级
  }

  /* 从 /api/library/sync/status 的响应里取出与当前论文对应的记录。
   * B 线端点仍在并行开发，这里对载荷形状做宽容解析：
   *   - 顶层数组，或 {items|records|entries|statuses: [...]}
   *   - 记录标识字段：paper_id / paper / id / doi 任一
   * 找不到对应记录返回 null（区块降级隐藏），绝不抛错。 */
  function extractSyncRecord(payload, paper) {
    if (!payload) return null;
    var list = Array.isArray(payload) ? payload
      : payload.items || payload.records || payload.entries || payload.statuses;
    if (!Array.isArray(list)) return null;
    var pid = paper && paper.id != null ? String(paper.id) : null;
    var doi = paper && paper.doi ? String(paper.doi) : null;
    for (var i = 0; i < list.length; i++) {
      var r = list[i];
      if (!r || typeof r !== 'object') continue;
      var rid = r.paper_id != null ? String(r.paper_id)
        : r.paper != null && typeof r.paper === 'object' ? (r.paper.id != null ? String(r.paper.id) : null)
        : r.id != null ? String(r.id) : null;
      var rdoi = r.doi ? String(r.doi) : (r.paper && r.paper.doi ? String(r.paper.doi) : null);
      if (pid && rid === pid) return normalize(r);
      if (doi && rdoi && rdoi.toLowerCase() === doi.toLowerCase()) return normalize(r);
    }
    return null;
  }

  function normalize(r) {
    return {
      local_saved: !!r.local_saved,
      zotero_state: Object.prototype.hasOwnProperty.call(SYNC_STATE_TEXT, r.zotero_state) ? r.zotero_state : null,
      last_error: r.last_error == null ? null : String(r.last_error)
    };
  }

  /* ---- §3.1(v04) 字段补全结果渲染（纯函数，Node 可测） ---- */

  function enrichErrorText(code) {
    if (code == null || code === '') return null;
    return ZOTERO_ERROR_TEXT[code] || null; // 未知错误码由调用方回退显示原始 message
  }

  function fieldLabel(field) {
    return FIELD_LABELS[field] || String(field);
  }

  /* 单字段补全结果 → 一行说明文本。
   * entry: {before:"missing|present", after:"filled|missing|skipped", origin, text_len}
   * before/after/origin 按契约 3.1 显示；origin 为 null（skipped）时不臆造来源。 */
  function enrichFieldLine(field, entry) {
    if (!entry || typeof entry !== 'object') return null;
    var label = fieldLabel(field);
    var before = entry.before === 'present' ? '已有' : '缺失';
    var origin = entry.origin && typeof entry.origin === 'string' ? (originLabel(entry.origin) || entry.origin) : null;
    var tail = origin ? '（来源：' + origin + '）' : '';
    if (entry.after === 'filled') return label + '：' + before + ' → 已补全' + tail;
    if (entry.after === 'skipped') return label + '：已有内容，跳过（不覆盖、不重复生成）';
    return label + '：' + before + ' → 仍未补全' + tail;
  }

  /* 补全结果汇总行。errors 形状：[{field, code, message}]。
   * 有 filled 且仍有 missing/skipped-未补全 → “部分完成 + 剩余待办”（契约 3.7）。 */
  function enrichSummaryLine(result) {
    if (!result || typeof result !== 'object' || !result.fields) return null;
    var filled = 0, remaining = 0;
    Object.keys(result.fields).forEach(function (f) {
      var e = result.fields[f];
      if (e && e.after === 'filled') filled++;
      else remaining++;
    });
    var errCount = Array.isArray(result.errors) ? result.errors.length : 0;
    if (filled > 0 && remaining > 0) {
      return '部分完成：已补全 ' + filled + ' 项，剩余待办 ' + remaining + ' 项' +
        (errCount ? '（含 ' + errCount + ' 项来源错误，见下方说明）' : '') + '。';
    }
    if (filled > 0) return '全部完成：本次补全 ' + filled + ' 项' + (errCount ? '，另有 ' + errCount + ' 项来源错误' : '') + '。';
    if (errCount) return '本次未补全任何字段：' + errCount + ' 项来源不可用（详见各字段说明）。';
    return '没有需要补全的缺失字段。';
  }

  /* ---- §3.6 内容状态说明（V4-08 修复）：依据真正保存的字段产物分类 ----
   * entry 取自 /api/ai/briefs* 响应 data.reuse.fields.<field>：
   *   {reused, origin, kind, kind_label, label, text, display_source}
   * 返回五分类之一（或 missing），界面据此写状态说明，绝不照抄后端
   * “缺失（等待补充）”这类与四要素生成状态自相矛盾的旧注记。 */
  function contentStatusCategory(entry) {
    if (!entry || typeof entry !== 'object') return 'missing';
    var origin = entry.origin;
    if (entry.reused) {
      if (origin === 'library_human') return 'human';
      if (origin === 'library_unknown_source') return 'unknown_source';
      if (origin === 'ai_with_source') return 'ai_with_source';
      if (entry.kind === 'translation') return 'translation';
      if (entry.kind === 'digest') return 'digest';
      if (entry.kind === 'summary') return 'summary_saved';
      return 'existing';
    }
    if (entry.display_source === 'ai_generated_this_app') {
      return entry.kind === 'translation' ? 'translation' : 'digest';
    }
    return 'missing';
  }

  var CONTENT_STATUS_TEXT = {
    human: '已有人工内容（人工录入的译文/总结，优先显示，AI 不覆盖）',
    unknown_source: '旧来源未知内容（库内已有，来源信息未记录，不伪造来源）',
    ai_with_source: 'AI 有来源结果（来源哈希已核对，可复核）',
    translation: '原摘要译文（完整译文，与摘要概括区分）',
    digest: '摘要概括（概括性中文摘要，非完整译文）',
    summary_saved: '已保存的总结（不作为原文摘要显示）',
    existing: '已有内容（优先复用，未重新生成）',
    missing: '本篇暂无已保存的该字段产物'
  };
  function contentStatusText(category, field) {
    if (category === 'translation' && field === 'title_zh') return '题名翻译（与摘要译文区分）';
    return CONTENT_STATUS_TEXT[category] || null;
  }

  var api = {
    ORIGIN_TEXT: ORIGIN_TEXT,
    SYNC_STATE_TEXT: SYNC_STATE_TEXT,
    ZOTERO_ERROR_TEXT: ZOTERO_ERROR_TEXT,
    FIELD_LABELS: FIELD_LABELS,
    CONTENT_STATUS_TEXT: CONTENT_STATUS_TEXT,
    originLabel: originLabel,
    syncStateText: syncStateText,
    extractSyncRecord: extractSyncRecord,
    enrichErrorText: enrichErrorText,
    fieldLabel: fieldLabel,
    enrichFieldLine: enrichFieldLine,
    enrichSummaryLine: enrichSummaryLine,
    contentStatusCategory: contentStatusCategory,
    contentStatusText: contentStatusText
  };

  root.WenxianExisting = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api; // Node 自测
})(typeof window !== 'undefined' ? window : globalThis);
