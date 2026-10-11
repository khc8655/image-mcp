// 识图与 OCR 业务微模块 (极简顺序调度与全生命周期模型运维)
import { esc, showToast } from './common.js?v=3.1.0';

export function renderVisionTab(appConfig) {
  const vis = appConfig.vision || {};

  const timeoutEl = document.getElementById('cfg_vis_timeout');
  if (timeoutEl) timeoutEl.value = vis.timeout_seconds || 45;

  const presetEl = document.getElementById('cfg_vision_preset');
  if (presetEl) presetEl.value = vis.default_preset || 'standard';

  const rapidEl = document.getElementById('cfg_vision_rapidocr');
  if (rapidEl) rapidEl.checked = vis.rapidocr_enabled !== false;

  // 渲染活跃识图模型池表格
  const tbody = document.getElementById('visionModelsTbody');
  if (!tbody) return;
  const models = vis.models || [];
  if (models.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:24px;">暂未挂载多模态识图模型，请点击右上角「+ 挂载识图模型」</td></tr>`;
  } else {
    tbody.innerHTML = models.map((m, idx) => {
      const currentRole = m.role || 'general';
      const roleSelectHtml = `
        <select class="select-inline font-bold" style="padding:4px 8px;font-size:12px;border-radius:4px;border:1px solid var(--khc-border-subtle);background:var(--khc-surface-base);" onchange="window.updateVisionModelRole(${idx}, this.value)">
          <option value="general" ${currentRole === 'general' ? 'selected' : ''}>general (通用视觉)</option>
          <option value="document" ${currentRole === 'document' ? 'selected' : ''}>document (高精文档表格)</option>
          <option value="gui" ${currentRole === 'gui' ? 'selected' : ''}>gui (坐标定位界面)</option>
          <option value="ocr" ${currentRole === 'ocr' ? 'selected' : ''}>ocr (纯字符提取)</option>
          <option value="fast" ${currentRole === 'fast' ? 'selected' : ''}>fast (轻量极速)</option>
        </select>
      `;

      return `
        <tr>
          <td style="text-align:center;">
            <input type="number" 
                   class="order-input" 
                   min="1" 
                   max="${models.length}" 
                   value="${idx + 1}" 
                   title="修改数字后按回车调整优先级" 
                   onchange="window.reorderVisionModel(${idx}, this.value)" 
                   onkeydown="if(event.key==='Enter')this.blur();">
          </td>
          <td class="mono-text font-bold" style="color:var(--khc-primary);">${esc(m.model)}</td>
          <td>${esc(m.provider)}</td>
          <td>${roleSelectHtml}</td>
          <td>
            <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
              <input type="checkbox" ${m.enabled ? 'checked' : ''} onchange="window.toggleVisionModel(${idx}, this.checked)">
              <span style="font-size:12px;">${m.enabled ? '启用' : '禁用'}</span>
            </label>
          </td>
          <td style="text-align:right;">
            <div style="display:inline-flex;gap:6px;align-items:center;">
              <button class="btn btn-secondary btn-small" type="button" onclick="window.openEditVisionModel(${idx})">
                <svg class="icon"><use href="#icon-edit"/></svg>
                <span>编辑</span>
              </button>
              <button class="btn btn-secondary btn-small" type="button" onclick="window.deleteVisionModel(${idx})" style="color:var(--color-error);">
                <svg class="icon"><use href="#icon-trash"/></svg>
                <span>移除</span>
              </button>
            </div>
          </td>
        </tr>
      `;
    }).join('');
  }
}

export function reorderVisionModel(fromIdx, targetPos, appConfig, onSaveFn) {
  const models = appConfig?.vision?.models;
  if (!models || models.length <= 1) return;
  const parsed = parseInt(targetPos, 10);
  if (isNaN(parsed)) {
    renderVisionTab(appConfig);
    return;
  }
  const toIdx = Math.max(0, Math.min(models.length - 1, parsed - 1));
  if (fromIdx === toIdx) {
    renderVisionTab(appConfig);
    return;
  }
  const [moved] = models.splice(fromIdx, 1);
  models.splice(toIdx, 0, moved);
  renderVisionTab(appConfig);
  showToast(`已将「${moved.model}」优先级调整至第 ${toIdx + 1} 位`, 'info');
  if (onSaveFn) onSaveFn();
}

export function updateVisionModelKey(idx, newKey, appConfig, onSaveFn) {
  if (appConfig?.vision?.models?.[idx]) {
    appConfig.vision.models[idx].key = newKey;
    showToast(`绑定 Key 已更新为 ${newKey}`, 'info');
    if (onSaveFn) onSaveFn();
  }
}

export function updateVisionModelRole(idx, newRole, appConfig, onSaveFn) {
  if (appConfig?.vision?.models?.[idx]) {
    appConfig.vision.models[idx].role = newRole;
    showToast(`角色已更新为 ${newRole}`, 'info');
    if (onSaveFn) onSaveFn();
  }
}

export function toggleVisionModel(idx, checked, appConfig, onSaveFn) {
  if (appConfig?.vision?.models?.[idx]) {
    appConfig.vision.models[idx].enabled = checked;
    renderVisionTab(appConfig);
    if (onSaveFn) onSaveFn();
  }
}

export function deleteVisionModel(idx, appConfig, onSaveFn) {
  if (!confirm('确认从活跃识图模型池中移除该模型？')) return;
  appConfig.vision.models.splice(idx, 1);
  renderVisionTab(appConfig);
  showToast('已从识图模型池移除', 'info');
  if (onSaveFn) onSaveFn();
}

export function collectVisionConfig(appConfig) {
  if (!appConfig.vision) appConfig.vision = {};
  appConfig.vision.timeout_seconds = parseInt(document.getElementById('cfg_vis_timeout')?.value, 10) || 45;
  appConfig.vision.default_preset = document.getElementById('cfg_vision_preset')?.value || 'standard';
  appConfig.vision.rapidocr_enabled = document.getElementById('cfg_vision_rapidocr')?.checked !== false;
  appConfig.vision.default_mode = 'sequential';
}

// 兼容旧版调用
export function selectVisMode() {}
