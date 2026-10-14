// KHC Image MCP 控制台前端主入口微模块
import { showToast, switchTab, openModal, closeModal } from './common.js?v=3.2.0';
import { api, getStoredToken, setStoredToken } from './api.js?v=3.2.0';
import { renderGenerationTab, selectGenMode, reorderGenModel, updateGenModelKey, updateGenModelRole, toggleGenModel, deleteGenModel, collectGenerationConfig, refreshStorageInfo, cleanStorageCache } from './tab_generation.js?v=3.2.0';
import { renderVisionTab, selectVisMode, reorderVisionModel, updateVisionModelKey, updateVisionModelRole, toggleVisionModel, deleteVisionModel, collectVisionConfig } from './tab_vision.js?v=3.2.0';
import { renderSearchTab, runSearchTest, collectSearchConfig } from './tab_search.js?v=3.2.0';
import { renderSystemTab, testVaultConnectivity, syncAllVaultCredentials, saveAdminToken, collectSystemConfig } from './tab_system.js?v=3.2.0';
import { openBindModelModal, onBindProviderChange, pickModelCard, confirmBindModel, openEditModelModal, confirmEditModel } from './modals.js?v=3.2.0';

let appConfig = null;
let vaultManifest = null;
let allPresetsData = null;

async function getVaultManifestSilent() {
  if (!vaultManifest) {
    try {
      vaultManifest = await api.getVaultManifest();
    } catch (_) {}
  }
  return vaultManifest;
}

async function getPresetsDataSilent() {
  if (!allPresetsData) {
    try {
      allPresetsData = await api.getPresets();
    } catch (_) {}
  }
  return allPresetsData;
}

function showLogin() {
  document.getElementById('loginView').style.display = 'block';
  document.getElementById('mainView').style.display = 'none';
  document.getElementById('navActions').style.display = 'none';
  setTimeout(() => document.getElementById('loginTokenInput')?.focus(), 50);
}

function showMain() {
  document.getElementById('loginView').style.display = 'none';
  document.getElementById('mainView').style.display = 'block';
  document.getElementById('navActions').style.display = 'flex';
}

async function doLogin() {
  const input = document.getElementById('loginTokenInput');
  const val = (input?.value || '').trim();
  if (!val) {
    showToast('请输入鉴权 Token', 'warning');
    return;
  }
  setStoredToken(val);
  await loadConfig();
}

function logout() {
  setStoredToken('');
  showLogin();
  showToast('已安全退出控制台', 'info');
}

async function loadConfig() {
  try {
    appConfig = await api.getConfig();
    showMain();
    renderAll();
    await getVaultManifestSilent();
    await getPresetsDataSilent();
  } catch (e) {
    if (e.message === 'UNAUTHORIZED') {
      showToast('Token 校验失败，请重新登录', 'error');
    } else {
      showToast('加载配置失败: ' + e.message, 'error');
    }
    showLogin();
  }
}

function renderAll() {
  if (!appConfig) return;
  renderGenerationTab(appConfig);
  renderVisionTab(appConfig);
  renderSearchTab(appConfig);
  renderSystemTab(appConfig);
}

async function saveAllConfig() {
  if (!appConfig) return;
  collectGenerationConfig(appConfig);
  collectVisionConfig(appConfig);
  collectSearchConfig(appConfig);
  collectSystemConfig(appConfig);

  const btn = document.getElementById('btnTopSave');
  if (btn) btn.disabled = true;

  try {
    const data = await api.saveConfig(appConfig);
    appConfig = data.config;
    renderAll();
    showToast('所有配置已成功保存至服务端', 'success');
  } catch (e) {
    showToast('保存配置异常: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function reloadAllPresets() {
  const btn = document.getElementById('btnTopReloadPresets');
  if (btn) btn.disabled = true;
  try {
    const data = await api.reloadPresets();
    allPresetsData = data.all_presets;
    appConfig = data.config;
    renderAll();
    showToast(data.message || '适配规则刷新成功', 'success');
  } catch (e) {
    showToast('刷新适配规则异常: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 绑定全局 window 供 HTML 事件直接调用
window.switchTab = switchTab;
window.openModal = openModal;
window.closeModal = closeModal;
window.doLogin = doLogin;
window.logout = logout;
window.saveAllConfig = saveAllConfig;
window.reloadAllPresets = reloadAllPresets;

// 生图模块导出
window.selectGenMode = (mode) => selectGenMode(mode, appConfig);
window.reorderGenModel = (fromIdx, pos) => reorderGenModel(fromIdx, pos, appConfig, saveAllConfig);
window.updateGenModelKey = (idx, key) => updateGenModelKey(idx, key, appConfig, saveAllConfig);
window.updateGenModelRole = (idx, role) => updateGenModelRole(idx, role, appConfig, saveAllConfig);
window.toggleGenModel = (idx, checked) => toggleGenModel(idx, checked, appConfig, saveAllConfig);
window.deleteGenModel = (idx) => deleteGenModel(idx, appConfig, saveAllConfig);
window.openEditGenModel = (idx) => openEditModelModal('generation', idx, appConfig);
window.refreshStorageInfo = refreshStorageInfo;
window.cleanStorageCache = cleanStorageCache;

// 识图模块导出
window.selectVisMode = (mode) => selectVisMode(mode, appConfig);
window.reorderVisionModel = (fromIdx, pos) => reorderVisionModel(fromIdx, pos, appConfig, saveAllConfig);
window.updateVisionModelKey = (idx, key) => updateVisionModelKey(idx, key, appConfig, saveAllConfig);
window.updateVisionModelRole = (idx, role) => updateVisionModelRole(idx, role, appConfig, saveAllConfig);
window.toggleVisionModel = (idx, checked) => toggleVisionModel(idx, checked, appConfig, saveAllConfig);
window.deleteVisionModel = (idx) => deleteVisionModel(idx, appConfig, saveAllConfig);
window.openEditVisionModel = (idx) => openEditModelModal('vision', idx, appConfig);

// 搜图模块导出
window.runSearchTest = runSearchTest;

// 系统模块导出
window.testVaultConnectivity = testVaultConnectivity;
window.syncAllVaultCredentials = () => syncAllVaultCredentials((newCfg) => { appConfig = newCfg; renderAll(); });
window.saveAdminToken = () => saveAdminToken(appConfig, saveAllConfig);

// 弹窗模块导出
window.openBindModelModal = (targetType) => openBindModelModal(targetType, getVaultManifestSilent, getPresetsDataSilent, appConfig);
window.onBindProviderChange = onBindProviderChange;
window.pickModelCard = pickModelCard;
window.confirmBindModel = () => confirmBindModel(saveAllConfig);
window.confirmEditModel = () => confirmEditModel(saveAllConfig);

window.addEventListener('DOMContentLoaded', () => {
  const tk = getStoredToken();
  if (!tk) {
    showLogin();
  } else {
    loadConfig();
  }
});
