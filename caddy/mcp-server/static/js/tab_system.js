// 系统与全局凭据资产微模块
import { esc, showToast, openModal, closeModal } from './common.js?v=3.1.0';
import { api, setStoredToken } from './api.js?v=3.1.0';


export function renderSystemTab(appConfig) {
  const v = appConfig.edgeone_vault || {};
  const urlEl = document.getElementById('cfg_vault_url');
  if (urlEl) urlEl.value = v.url || '';
  const tokenEl = document.getElementById('cfg_vault_token');
  if (tokenEl) tokenEl.value = v.token || '';

  // 渲染模型适配规则库 Presets 状态卡片列表
  const presetsList = document.getElementById('presetsStatusList');
  if (presetsList) {
    const installed = appConfig.installed_presets || [];
    if (installed.length === 0) {
      presetsList.innerHTML = `<span class="muted">未检测到已加载的适配预设</span>`;
    } else {
      presetsList.innerHTML = installed.map(p => {
        const imgCount = (p.image_models || []).length;
        const visCount = (p.vision_models || []).length;
        return `
          <div class="mode-card active" style="margin:0;padding:10px 14px;flex:1 1 200px;">
            <div style="display:flex;align-items:center;justify-content:space-between;">
              <span class="font-bold">${esc(p.name)}</span>
              <span class="badge b-done">v${esc(p.version || '1.3')}</span>
            </div>
            <div class="muted mono-text" style="font-size:11px;margin-top:4px;">ID: ${esc(p.id)}</div>
            <div style="display:flex;gap:6px;margin-top:6px;">
              ${p.has_image ? `<span class="badge b-running" style="font-size:10px;">生图: ${imgCount}款</span>` : ''}
              ${p.has_vision ? `<span class="badge b-waiting" style="font-size:10px;">识图: ${visCount}款</span>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }
  }

  // Admin Token
  const adminTkEl = document.getElementById('cfg_admin_token');
  if (adminTkEl) adminTkEl.value = appConfig?.auth?.admin_token || '';
}

export async function testVaultConnectivity() {
  const url = document.getElementById('cfg_vault_url')?.value.trim() || '';
  const token = document.getElementById('cfg_vault_token')?.value.trim() || '';
  const btn = document.getElementById('btnTestVault');
  const alertBox = document.getElementById('vaultDiagAlert');

  if (btn) btn.disabled = true;
  if (alertBox) alertBox.style.display = 'none';

  try {
    const data = await api.testVault(url, token);
    if (alertBox) {
      alertBox.style.display = 'block';
      alertBox.className = 'alert success';
      alertBox.innerHTML = `
        <svg class="icon"><use href="#icon-success"/></svg>
        <span class="message">连接成功！耗时: ${data.latency_ms}ms · 发现 ${data.providers_count} 家厂商 (${(data.provider_ids || []).slice(0, 5).join(', ')}...)</span>
      `;
    }
    showToast('EdgeOne 凭据中枢连通成功', 'success');
  } catch (e) {
    if (alertBox) {
      alertBox.style.display = 'block';
      alertBox.className = 'alert error';
      alertBox.innerHTML = `
        <svg class="icon"><use href="#icon-error"/></svg>
        <span class="message">连接失败: ${esc(e.message)}</span>
      `;
    }
    showToast('中枢连接失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function syncAllVaultCredentials(onReload) {
  const btn = document.getElementById('btnSyncAllVault');
  if (btn) btn.disabled = true;
  showToast('正在从 EdgeOne 中枢全量同步所有授权凭据...', 'info');

  try {
    const data = await api.syncVaultCredentials();
    showToast(data.message || '全量凭据同步成功', 'success');
    if (onReload) onReload(data.config);
  } catch (e) {
    showToast('全量凭据同步异常: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function saveAdminToken(appConfig, saveConfigFn) {
  const val = document.getElementById('cfg_admin_token')?.value.trim();
  if (!val) {
    showToast('Admin Token 不能为空', 'warning');
    return;
  }
  if (!appConfig.auth) appConfig.auth = {};
  appConfig.auth.admin_token = val;
  const ok = await saveConfigFn();
  if (ok) {
    setStoredToken(val);
    showToast('Admin Token 已更新', 'success');
  }
}

export function collectSystemConfig(appConfig) {
  if (!appConfig.edgeone_vault) appConfig.edgeone_vault = {};
  appConfig.edgeone_vault.url = document.getElementById('cfg_vault_url')?.value.trim() || '';
  const tk = document.getElementById('cfg_vault_token')?.value.trim();
  if (tk && !tk.includes('*')) {
    appConfig.edgeone_vault.token = tk;
  }
}

// 兼容性导出垫片（防止 CDN 或旧客户端缓存 app.js 触发 SyntaxError）
export function openPullProviderModal() {}
export function onPullProviderChange() {}
export function confirmPullProvider() {}
export function probeProviderUpstream() {}
export function pullProviderKey() {}

