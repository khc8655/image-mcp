// 生图业务微模块 (极简顺序调度与全生命周期模型运维)
import { esc, showToast } from './common.js?v=2.9.0';

export function renderGenerationTab(appConfig) {
  const gen = appConfig.generation || {};

  const timeoutEl = document.getElementById('cfg_gen_timeout');
  if (timeoutEl) timeoutEl.value = gen.timeout_seconds || 45;

  const resEl = document.getElementById('cfg_gen_resolution');
  if (resEl) resEl.value = gen.default_resolution || '2K';

  const aspectEl = document.getElementById('cfg_gen_aspect');
  if (aspectEl) aspectEl.value = gen.default_aspect_ratio || '16:9';

  const qualityEl = document.getElementById('cfg_gen_quality');
  if (qualityEl) qualityEl.value = gen.default_quality || 'auto';

  const groundEl = document.getElementById('cfg_gen_grounding');
  if (groundEl) groundEl.value = gen.default_grounding ? 'true' : 'false';

  const countEl = document.getElementById('cfg_gen_count');
  if (countEl) countEl.value = gen.default_count || 1;

  // 渲染活跃模型池表格
  const tbody = document.getElementById('genModelsTbody');
  if (!tbody) return;
  const models = gen.models || [];
  if (models.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:24px;">暂未挂载任何生图模型，请点击右上角「+ 挂载生图模型」</td></tr>`;
  } else {
    tbody.innerHTML = models.map((m, idx) => {
      const currentRole = m.role || 'general';
      const roleSelectHtml = `
        <select class="select-inline font-bold" style="padding:4px 8px;font-size:12px;border-radius:4px;border:1px solid var(--khc-border-subtle);background:var(--khc-surface-base);" onchange="window.updateGenModelRole(${idx}, this.value)">
          <option value="general" ${currentRole === 'general' ? 'selected' : ''}>general (通用画质)</option>
          <option value="portrait" ${currentRole === 'portrait' ? 'selected' : ''}>portrait (写真肖像)</option>
          <option value="chinese" ${currentRole === 'chinese' ? 'selected' : ''}>chinese (国风排版)</option>
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
                   onchange="window.reorderGenModel(${idx}, this.value)" 
                   onkeydown="if(event.key==='Enter')this.blur();">
          </td>
          <td class="mono-text font-bold" style="color:var(--khc-primary);">${esc(m.model)}</td>
          <td>${esc(m.provider)}</td>
          <td>${roleSelectHtml}</td>
          <td>
            <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
              <input type="checkbox" ${m.enabled ? 'checked' : ''} onchange="window.toggleGenModel(${idx}, this.checked)">
              <span style="font-size:12px;">${m.enabled ? '启用' : '禁用'}</span>
            </label>
          </td>
          <td style="text-align:right;">
            <div style="display:inline-flex;gap:6px;align-items:center;">
              <button class="btn btn-secondary btn-small" type="button" onclick="window.openEditGenModel(${idx})">
                <svg class="icon"><use href="#icon-edit"/></svg>
                <span>编辑</span>
              </button>
              <button class="btn btn-secondary btn-small" type="button" onclick="window.deleteGenModel(${idx})" style="color:var(--color-error);">
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

export function reorderGenModel(fromIdx, targetPos, appConfig, onSaveFn) {
  const models = appConfig?.generation?.models;
  if (!models || models.length <= 1) return;
  const parsed = parseInt(targetPos, 10);
  if (isNaN(parsed)) {
    renderGenerationTab(appConfig);
    return;
  }
  const toIdx = Math.max(0, Math.min(models.length - 1, parsed - 1));
  if (fromIdx === toIdx) {
    renderGenerationTab(appConfig);
    return;
  }
  const [moved] = models.splice(fromIdx, 1);
  models.splice(toIdx, 0, moved);
  renderGenerationTab(appConfig);
  showToast(`已将「${moved.model}」优先级调整至第 ${toIdx + 1} 位`, 'info');
  if (onSaveFn) onSaveFn();
}

export function updateGenModelKey(idx, newKey, appConfig, onSaveFn) {
  if (appConfig?.generation?.models?.[idx]) {
    appConfig.generation.models[idx].key = newKey;
    showToast(`绑定 Key 已更新为 ${newKey}`, 'info');
    if (onSaveFn) onSaveFn();
  }
}

export function updateGenModelRole(idx, newRole, appConfig, onSaveFn) {
  if (appConfig?.generation?.models?.[idx]) {
    appConfig.generation.models[idx].role = newRole;
    showToast(`角色已更新为 ${newRole}`, 'info');
    if (onSaveFn) onSaveFn();
  }
}

export function toggleGenModel(idx, checked, appConfig, onSaveFn) {
  if (appConfig?.generation?.models?.[idx]) {
    appConfig.generation.models[idx].enabled = checked;
    renderGenerationTab(appConfig);
    if (onSaveFn) onSaveFn();
  }
}

export function deleteGenModel(idx, appConfig, onSaveFn) {
  if (!confirm('确认从活跃生图模型池中移除该模型？')) return;
  appConfig.generation.models.splice(idx, 1);
  renderGenerationTab(appConfig);
  showToast('已从生图模型池移除', 'info');
  if (onSaveFn) onSaveFn();
}

export function collectGenerationConfig(appConfig) {
  if (!appConfig.generation) appConfig.generation = {};
  appConfig.generation.timeout_seconds = parseInt(document.getElementById('cfg_gen_timeout')?.value, 10) || 45;
  appConfig.generation.default_resolution = document.getElementById('cfg_gen_resolution')?.value || '2K';
  appConfig.generation.default_aspect_ratio = document.getElementById('cfg_gen_aspect')?.value || '16:9';
  appConfig.generation.default_quality = document.getElementById('cfg_gen_quality')?.value || 'auto';
  appConfig.generation.default_grounding = document.getElementById('cfg_gen_grounding')?.value === 'true';
  appConfig.generation.default_count = Math.min(4, Math.max(1, parseInt(document.getElementById('cfg_gen_count')?.value, 10) || 1));
  appConfig.generation.default_mode = 'sequential';
}

// 兼容旧版调用
export function selectGenMode() {}
