/**
 * 外开链接协议白名单：shell.openExternal 只允许 http/https/mailto，
 * 拦截页面注入或拖放触发的 file:、javascript:、data: 等任意协议外开。
 */
const ALLOWED_PROTOCOLS = ['http:', 'https:', 'mailto:'];

function isAllowedExternalUrl(raw) {
  if (typeof raw !== 'string') return false;
  let parsed;
  try {
    parsed = new URL(raw.trim());
  } catch (_) {
    return false;
  }
  return ALLOWED_PROTOCOLS.includes(parsed.protocol);
}

module.exports = { isAllowedExternalUrl };
