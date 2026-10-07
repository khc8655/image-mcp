// 通用交互与 UI 辅助微模块

export function esc(str) {
  return String(str ?? '').replace(/[&<>"']/g, s => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[s]));
}

export function showToast(msg, type = 'info') {
  const stack = document.getElementById('toastStack');
  if (!stack) return;
  const el = document.createElement('div');
  const alertClass = type === 'error' ? 'error' : (type === 'warning' ? 'warning' : (type === 'success' ? 'success' : 'info'));
  const iconId = type === 'error' ? '#icon-error' : (type === 'warning' ? '#icon-warning' : (type === 'success' ? '#icon-success' : '#icon-info'));
  el.className = `alert ${alertClass}`;
  el.style.cssText = 'box-shadow:var(--shadow-md);min-width:280px;animation:fadeIn .2s ease;';
  el.innerHTML = `
    <svg class="icon"><use href="${iconId}"/></svg>
    <span class="message">${esc(msg)}</span>
  `;
  stack.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .3s';
    setTimeout(() => el.remove(), 300);
  }, 3500);
}

export function switchTab(tabId, btnEl) {
  document.querySelectorAll('.tabs-line .tab').forEach(t => t.classList.remove('active'));
  if (btnEl) btnEl.classList.add('active');
  document.querySelectorAll('.tab-section').forEach(s => s.style.display = 'none');
  const target = document.getElementById(`tab-${tabId}`);
  if (target) target.style.display = 'block';
}

export function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = 'flex';
}

export function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = 'none';
}
