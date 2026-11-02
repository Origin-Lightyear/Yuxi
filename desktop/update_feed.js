/**
 * 自动更新入口判定：更新源默认跟随当前服务地址，可用 YUXI_UPDATE_URL 覆盖。
 *
 * 只接受 http/https：file:、ftp: 等地址既不是更新分发形态，也不该交给 updater。
 */
const UPDATE_PATH_SUFFIX = '/desktop-updates';

function normalizeHttpUrl(raw) {
  if (typeof raw !== 'string') return null;
  let parsed;
  try {
    parsed = new URL(raw.trim());
  } catch (_) {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  // 丢掉 query/hash：更新源是目录，不是单次请求
  return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, '');
}

function resolveUpdateUrl({ envUrl, backendUrl, suffix = UPDATE_PATH_SUFFIX }) {
  const override = normalizeHttpUrl(envUrl);
  if (override) return override;
  const base = normalizeHttpUrl(backendUrl);
  if (!base) return null;
  // 更新包固定在站点根路径：网页部署的子路径不参与分发地址，避免客户端请求落到
  // 未配置的 `/<base>/desktop-updates/` 并回落到 SPA 首页
  return normalizeHttpUrl(`${new URL(base).origin}${suffix}`);
}

// Windows x64 是唯一分发平台；macOS 未签名，ARM 客户端不检查只含 x64 的更新源
function isAutoUpdateSupported({ platform, isPackaged, arch }) {
  return isPackaged === true && platform === 'win32' && arch === 'x64';
}

module.exports = { normalizeHttpUrl, resolveUpdateUrl, isAutoUpdateSupported };
