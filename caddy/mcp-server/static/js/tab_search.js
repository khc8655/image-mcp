// 搜图业务与在线测试微模块
import { esc, showToast } from './common.js?v=3.1.0';
import { api } from './api.js?v=3.1.0';


export function renderSearchTab(appConfig) {
  const src = appConfig.search || {};
  const engEl = document.getElementById('cfg_search_engine');
  if (engEl) engEl.value = src.default_text_engine || 'duckduckgo';

  const limitEl = document.getElementById('cfg_search_limit');
  if (limitEl) limitEl.value = src.max_results || 6;

  const keyEl = document.getElementById('cfg_pexels_key');
  if (keyEl) keyEl.value = src.pexels_api_key || '';

  const ocrEl = document.getElementById('cfg_search_use_ocr');
  if (ocrEl) ocrEl.checked = src.reverse_search_use_ocr !== false;
}

export async function runSearchTest() {
  const query = document.getElementById('test_search_query')?.value.trim() || '';
  const image = document.getElementById('test_search_image')?.value.trim() || '';
  const limit = parseInt(document.getElementById('test_search_limit')?.value, 10) || 6;

  if (!query && !image) {
    showToast('请输入关键词或参考图片', 'warning');
    return;
  }

  const btn = document.getElementById('btnRunSearchTest');
  const loading = document.getElementById('searchTestLoading');
  const box = document.getElementById('searchTestResults');

  if (btn) btn.disabled = true;
  if (loading) loading.style.display = 'block';
  if (box) box.innerHTML = '';

  try {
    const data = await api.testSearch(query, image, limit);
    const md = data.markdown || '';
    const lines = md.split('\n');
    const cards = [];
    let curr = null;

    for (const line of lines) {
      const titleMatch = line.match(/^\d+\.\s+\*\*\[(.*?)\]\((.*?)\)\*\*/);
      if (titleMatch) {
        curr = { title: titleMatch[1], origUrl: titleMatch[2], thumbUrl: '', meta: '' };
        cards.push(curr);
        continue;
      }
      const thumbMatch = line.match(/\[\!\[(.*?)\]\((.*?)\)\]\((.*?)\)/);
      if (thumbMatch && curr) {
        curr.thumbUrl = thumbMatch[2];
        continue;
      }
      if (line.includes('规格:') && curr) {
        curr.meta = line.replace('- 规格:', '').trim();
      }
    }

    if (box) {
      if (cards.length === 0) {
        box.innerHTML = `<div class="alert info" style="grid-column:1/-1;">${esc(md)}</div>`;
      } else {
        box.innerHTML = cards.map(c => `
          <div class="search-img-card">
            <a href="${esc(c.origUrl)}" target="_blank" rel="noopener">
              <img class="search-img-thumb" src="${esc(c.thumbUrl || c.origUrl)}" alt="${esc(c.title)}" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'200\\' height=\\'150\\' fill=\\'%23ddd\\'><text x=\\'50%\\' y=\\'50%\\' dominant-baseline=\\'middle\\' text-anchor=\\'middle\\' fill=\\'%23666\\'>图片预览</text></svg>'">
            </a>
            <div class="search-img-info">
              <a class="search-img-title" href="${esc(c.origUrl)}" target="_blank" rel="noopener" title="${esc(c.title)}">${esc(c.title)}</a>
              <div class="search-img-meta">
                <span>${esc(c.meta || '高分辨率')}</span>
              </div>
            </div>
          </div>
        `).join('');
      }
    }
    showToast(`检索完成，找到 ${cards.length || 0} 张相关图片`, 'success');
  } catch (e) {
    showToast('搜图测试异常: ' + e.message, 'error');
  } finally {
    if (loading) loading.style.display = 'none';
    if (btn) btn.disabled = false;
  }
}

export function collectSearchConfig(appConfig) {
  if (!appConfig.search) appConfig.search = {};
  appConfig.search.default_text_engine = document.getElementById('cfg_search_engine')?.value || 'duckduckgo';
  appConfig.search.max_results = parseInt(document.getElementById('cfg_search_limit')?.value, 10) || 6;
  appConfig.search.pexels_api_key = document.getElementById('cfg_pexels_key')?.value || '';
  appConfig.search.reverse_search_use_ocr = document.getElementById('cfg_search_use_ocr')?.checked !== false;
}
