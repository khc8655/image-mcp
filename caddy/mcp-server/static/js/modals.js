// 挂载与编辑模型弹窗微模块 (极简纯点选与全生命周期运维架构)
import { esc, showToast, openModal, closeModal } from './common.js?v=3.2.0';
import { api } from './api.js?v=3.2.0';

let currentManifest = null;
let currentPresets = null;
let currentConfig = null;

export async function openBindModelModal(targetType, getManifestFn, getPresetsFn, appConfig) {
  currentConfig = appConfig;
  document.getElementById('bind_target_type').value = targetType;
  document.getElementById('bindModalTitle').textContent = targetType === 'generation' ? '挂载生图模型' : '挂载识图模型';
  document.getElementById('bind_selected_model').value = '';
  const customEl = document.getElementById('bind_custom_model');
  if (customEl) customEl.value = '';

  const roleSel = document.getElementById('bind_role_select');
  if (roleSel) {
    if (targetType === 'generation') {
      roleSel.innerHTML = `
        <option value="general">general — 通用高画质与创意构图</option>
        <option value="portrait">portrait — 人物写真、肖像面部与主体参考</option>
        <option value="chinese">chinese — 中文书法排版、海报设计与国风</option>
      `;
    } else {
      roleSel.innerHTML = `
        <option value="general">general — 通用多模态视觉理解与通用分析</option>
        <option value="document">document — 高精文档、财务报表与 Markdown 表格还原</option>
        <option value="ocr">ocr — 快速字符提取与文本结构化分析</option>
        <option value="gui">gui — 界面元素识别与归一化坐标定位</option>
        <option value="fast">fast — 轻量极速分析</option>
      `;
    }
  }

  currentManifest = await getManifestFn();
  currentPresets = await getPresetsFn();

  const localIds = Object.keys(appConfig?.providers || {});
  const remoteProvs = currentManifest?.providers || {};
  const allIds = Array.from(new Set([...localIds, ...Object.keys(remoteProvs)]));

  const sel = document.getElementById('bind_provider_select');
  if (sel) {
    sel.innerHTML = allIds.map(id => {
      const name = appConfig?.providers?.[id]?.name || remoteProvs[id]?.name || id;
      return `<option value="${esc(id)}">${esc(name)} (${esc(id)})</option>`;
    }).join('');
  }

  onBindProviderChange();
  openModal('bindModelModal');
}

export function onBindProviderChange() {
  const pid = document.getElementById('bind_provider_select')?.value;
  const targetType = document.getElementById('bind_target_type')?.value;
  const cardsBox = document.getElementById('bind_model_cards');
  if (!pid || !cardsBox) return;

  // 获取可用模型列表 (无需关心 Key，调用时后端自动读取使用)
  let modelCards = [];
  if (targetType === 'generation') {
    // 生图：优先从本地适配规则库 Presets 提取经过入参/尺寸验证的官方模型
    const preset = currentPresets?.presets?.[pid] || {};
    const rawList = preset.image_models || [];
    modelCards = rawList.map(item => {
      if (typeof item === 'object' && item !== null) {
        return { id: item.name, desc: item.description || '' };
      }
      return { id: String(item), desc: '' };
    });
  } else {
    // 识图与 OCR：直接对接 EdgeOne 统一中枢，从 manifest 中读取该厂商配置的真实模型
    const remoteModels = currentManifest?.providers?.[pid]?.models || currentConfig?.providers?.[pid]?.models || [];
    if (remoteModels.length > 0) {
      modelCards = remoteModels.map(m => {
        const mid = typeof m === 'object' ? (m.name || m.id) : String(m);
        return { id: mid, desc: 'EdgeOne 统一多模态模型' };
      });
    }
    // 兜底：如果中枢暂未返回模型列表，读取预设或内置推荐
    if (modelCards.length === 0) {
      const preset = currentPresets?.presets?.[pid] || {};
      const pList = preset.vision_models || preset.recommended_models || [];
      modelCards = pList.map(item => ({ id: typeof item === 'object' ? item.name : String(item), desc: '官方推荐视觉模型' }));
    }
  }

  if (modelCards.length === 0) {
    cardsBox.innerHTML = `<div class="muted" style="padding:12px;text-align:center;">该厂商暂未发现可用${targetType === 'generation' ? '生图' : '识图'}模型</div>`;
    document.getElementById('bind_selected_model').value = '';
    return;
  }

  // 默认选中第一个
  document.getElementById('bind_selected_model').value = modelCards[0].id;

  cardsBox.innerHTML = modelCards.map((m, idx) => `
    <div class="mode-card ${idx === 0 ? 'active' : ''}" style="margin:0;padding:10px 14px;cursor:pointer;" onclick="window.pickModelCard('${esc(m.id)}', this)">
      <div style="display:flex;align-items:center;justify-content:space-between;width:100%;">
        <div class="mono-text font-bold" style="font-size:13px;color:var(--khc-primary);">${esc(m.id)}</div>
        <span class="badge b-done" style="font-size:11px;">可用</span>
      </div>
      ${m.desc ? `<div class="muted" style="font-size:12px;margin-top:4px;line-height:1.4;">${esc(m.desc)}</div>` : ''}
    </div>
  `).join('');
}

export function pickModelCard(modelId, cardEl) {
  document.getElementById('bind_selected_model').value = modelId;
  document.querySelectorAll('#bind_model_cards .mode-card').forEach(c => c.classList.remove('active'));
  if (cardEl) cardEl.classList.add('active');
}

export async function confirmBindModel(saveConfigFn) {
  const targetType = document.getElementById('bind_target_type')?.value;
  const provider = document.getElementById('bind_provider_select')?.value;
  const custom = document.getElementById('bind_custom_model')?.value.trim();
  const model = custom || document.getElementById('bind_selected_model')?.value.trim();
  const role = document.getElementById('bind_role_select')?.value;

  if (!provider || !model) {
    showToast('请选择要挂载的模型', 'warning');
    return;
  }

  const btn = document.getElementById('btnConfirmBindModel');
  if (btn) btn.disabled = true;

  try {
    // 写入模型池 (调用时由后端自动匹配可用有效 Key)
    if (targetType === 'generation') {
      if (!currentConfig.generation) currentConfig.generation = { models: [] };
      if (!Array.isArray(currentConfig.generation.models)) currentConfig.generation.models = [];
      const exists = currentConfig.generation.models.some(m => m.provider === provider && m.model === model);
      if (exists) {
        showToast(`生图模型池中已存在「${model}」`, 'warning');
        return;
      }
      currentConfig.generation.models.push({
        model,
        provider,
        role: role || 'general',
        enabled: true
      });
    } else {
      if (!currentConfig.vision) currentConfig.vision = { models: [] };
      if (!Array.isArray(currentConfig.vision.models)) currentConfig.vision.models = [];
      const exists = currentConfig.vision.models.some(m => m.provider === provider && m.model === model);
      if (exists) {
        showToast(`识图模型池中已存在「${model}」`, 'warning');
        return;
      }
      currentConfig.vision.models.push({
        model,
        provider,
        role: role || 'general',
        enabled: true
      });
    }

    closeModal('bindModelModal');
    await saveConfigFn();
    showToast(`已成功将「${model}」挂载至${targetType === 'generation' ? '生图' : '识图'}模型池`, 'success');
  } catch (e) {
    showToast('挂载失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// -------------------------------------------------------------
// 编辑模型配置 (Key、角色、高级参数)
// -------------------------------------------------------------
export function openEditModelModal(targetType, idx, appConfig) {
  currentConfig = appConfig;
  const list = targetType === 'generation' ? (appConfig?.generation?.models || []) : (appConfig?.vision?.models || []);
  const item = list[idx];
  if (!item) return;

  document.getElementById('edit_target_type').value = targetType;
  document.getElementById('edit_model_index').value = idx;
  document.getElementById('edit_model_id').value = item.model;
  document.getElementById('edit_model_provider').value = item.provider;

  // 1. 填充角色
  const roleSel = document.getElementById('edit_role_select');
  if (roleSel) {
    if (targetType === 'generation') {
      roleSel.innerHTML = `
        <option value="general" ${item.role === 'general' ? 'selected' : ''}>general — 通用画质</option>
        <option value="portrait" ${item.role === 'portrait' ? 'selected' : ''}>portrait — 写真肖像</option>
        <option value="chinese" ${item.role === 'chinese' ? 'selected' : ''}>chinese — 国风排版</option>
      `;
    } else {
      roleSel.innerHTML = `
        <option value="general" ${item.role === 'general' ? 'selected' : ''}>general — 通用视觉理解</option>
        <option value="document" ${item.role === 'document' ? 'selected' : ''}>document — 高精文档表格</option>
        <option value="gui" ${item.role === 'gui' ? 'selected' : ''}>gui — 界面坐标定位</option>
        <option value="ocr" ${item.role === 'ocr' ? 'selected' : ''}>ocr — 纯字符提取</option>
        <option value="fast" ${item.role === 'fast' ? 'selected' : ''}>fast — 轻量极速</option>
      `;
    }
  }

  // 2. 高级参数面板动态适配
  const advSection = document.getElementById('editAdvancedSection');
  const advMinimax = document.getElementById('editAdvMinimax');
  const advAgnes = document.getElementById('editAdvAgnes');
  const advGoogle = document.getElementById('editAdvGoogle');

  if (advSection && advMinimax && advAgnes && advGoogle) {
    advMinimax.style.display = 'none';
    advAgnes.style.display = 'none';
    advGoogle.style.display = 'none';

    const adv = item.advanced || {};
    if (targetType === 'generation') {
      if (item.provider === 'minimax') {
        advSection.style.display = 'block';
        advMinimax.style.display = 'flex';
        document.getElementById('adv_minimax_optimizer').value =
          adv.prompt_optimizer === true ? 'on' : adv.prompt_optimizer === false ? 'off' : 'auto';
        document.getElementById('adv_minimax_watermark').checked = adv.aigc_watermark === true;
      } else if (item.provider === 'agnes' || item.provider === 'siliconflow') {
        advSection.style.display = 'block';
        advAgnes.style.display = 'flex';
        document.getElementById('adv_agnes_size').value = adv.size || '2K';
      } else if (item.provider === 'google') {
        advSection.style.display = 'block';
        advGoogle.style.display = 'flex';
        document.getElementById('adv_google_image_size').value = adv.image_size || '2K';
        document.getElementById('adv_google_person').value = adv.person_generation || 'ALLOW_ADULT';
        document.getElementById('adv_google_thinking').value = adv.thinking_level || '';
        document.getElementById('adv_google_image_search').checked = adv.image_search === true;
      } else {
        advSection.style.display = 'none';
      }
    } else {
      advSection.style.display = 'none';
    }
  }

  openModal('editModelModal');
}

export async function confirmEditModel(saveConfigFn) {
  const targetType = document.getElementById('edit_target_type')?.value;
  const idx = parseInt(document.getElementById('edit_model_index')?.value, 10);
  if (isNaN(idx) || idx < 0) return;

  const list = targetType === 'generation' ? (currentConfig?.generation?.models || []) : (currentConfig?.vision?.models || []);
  const item = list[idx];
  if (!item) return;

  const newModelId = document.getElementById('edit_model_id')?.value.trim();
  if (!newModelId) {
    showToast('模型 ID 不能为空', 'warning');
    return;
  }
  if (newModelId !== item.model && list.some((m, i) => i !== idx && m.provider === item.provider && m.model === newModelId)) {
    showToast(`模型池中已存在「${newModelId}」`, 'warning');
    return;
  }
  item.model = newModelId;

  const newRole = document.getElementById('edit_role_select')?.value || 'general';
  item.role = newRole;

  // 高级参数提取
  if (!item.advanced) item.advanced = {};
  if (item.provider === 'minimax') {
    const opt = document.getElementById('adv_minimax_optimizer')?.value || 'auto';
    if (opt === 'auto') delete item.advanced.prompt_optimizer;
    else item.advanced.prompt_optimizer = opt === 'on';
    item.advanced.aigc_watermark = document.getElementById('adv_minimax_watermark')?.checked === true;
  } else if (item.provider === 'agnes' || item.provider === 'siliconflow') {
    item.advanced.size = document.getElementById('adv_agnes_size')?.value || '2K';
    delete item.advanced.negative_prompt;
  } else if (item.provider === 'google') {
    item.advanced.image_size = document.getElementById('adv_google_image_size')?.value || '2K';
    item.advanced.person_generation = document.getElementById('adv_google_person')?.value || 'ALLOW_ADULT';
    const think = document.getElementById('adv_google_thinking')?.value || '';
    if (think) item.advanced.thinking_level = think;
    else delete item.advanced.thinking_level;
    item.advanced.image_search = document.getElementById('adv_google_image_search')?.checked === true;
  }

  closeModal('editModelModal');
  await saveConfigFn();
  showToast(`已成功保存「${item.model}」的配置`, 'success');
}

// 兼容性导出垫片（防止 CDN 或旧客户端缓存 app.js 触发 SyntaxError）
export function selectSingleKeyCapsule() {}
export function pickModelCapsule() {}
export function probeUpstreamModels() {}
