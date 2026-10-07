// API 网络请求微模块
export const TOKEN_STORAGE_KEY = 'khc_image_mcp_admin_token';

export function getStoredToken() {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || '';
}

export function setStoredToken(tk) {
  localStorage.setItem(TOKEN_STORAGE_KEY, (tk || '').trim());
}

export function getAuthHeaders() {
  return {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${getStoredToken()}`
  };
}

export const api = {
  async getConfig() {
    const res = await fetch('/api/config', { headers: getAuthHeaders() });
    if (res.status === 401) throw new Error('UNAUTHORIZED');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  async saveConfig(config) {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify(config)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '保存失败');
    return data;
  },

  async getPresets() {
    const res = await fetch('/api/presets', { headers: getAuthHeaders() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  async reloadPresets() {
    const res = await fetch('/api/presets/reload', {
      method: 'POST',
      headers: getAuthHeaders()
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '重载失败');
    return data;
  },

  async testVault(url, token) {
    const res = await fetch('/api/vault/test', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ edgeone_url: url, token })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '连接失败');
    return data;
  },

  async getVaultManifest() {
    const res = await fetch('/api/vault/manifest', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({})
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  async fetchVaultKey(provider, keys) {
    const res = await fetch('/api/vault/fetch-key', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ provider, keys })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '拉取失败');
    return data;
  },

  async syncVaultCredentials() {
    const res = await fetch('/api/vault/sync', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '同步失败');
    return data;
  },

  async probeModels(provider, keyLabel = '') {
    const res = await fetch('/api/vault/probe-models', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ provider, key_label: keyLabel })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '探测失败');
    return data;
  },

  async testSearch(query, image = '', limit = 6) {
    const res = await fetch('/api/search/test', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ query, image, limit })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '检索失败');
    return data;
  }
};
