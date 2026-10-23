/**
 * 窗口状态记忆：恢复上次布局前先校验，避免显示器拔除/分辨率变化后
 * 窗口落在所有屏幕之外不可见。state 来自 config.json（不可信持久化输入）。
 */
const MIN_WIDTH = 900;
const MIN_HEIGHT = 600;

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function intersects(x, y, width, height, d) {
  return x < d.x + d.width && x + width > d.x && y < d.y + d.height && y + height > d.y;
}

function sanitizeWindowState(state, displays) {
  if (!state) return null;
  const { x, y, width, height } = state;
  if (![x, y, width, height].every(isFiniteNumber)) return null;
  if (width < MIN_WIDTH || height < MIN_HEIGHT) return null;
  if (!displays.some((d) => intersects(x, y, width, height, d))) return null;
  return { x, y, width, height, maximized: !!state.maximized };
}

module.exports = { sanitizeWindowState };
