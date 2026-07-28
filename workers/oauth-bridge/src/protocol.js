export const CALLBACK_PATH = "/oauth/callback";
export const BRIDGE_PATH = "/oauth/bridge";
export const RESULT_PATH = "/oauth/result";
export const DELIVERY_PATH = "/_internal/deliver";

export function isOpaqueState(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{32,256}$/.test(value);
}

export function hasValidOAuthCallback(url) {
  const state = url.searchParams.get("state");
  const code = url.searchParams.get("code");
  const error = url.searchParams.get("error");
  return Boolean(isOpaqueState(state) && (Boolean(code) !== Boolean(error)));
}

export function callbackPayload(url) {
  const state = url.searchParams.get("state");
  const code = url.searchParams.get("code");
  const error = url.searchParams.get("error");
  if (!state || (Boolean(code) === Boolean(error))) {
    throw new Error("invalid OAuth callback");
  }
  return code ? { type: "oauth_code", state, code } : { type: "oauth_cancelled", state };
}

function debugValue(value) {
  return value === null || typeof value === "string" ? value : null;
}

export function debugIdentity(payload) {
  const value = payload?.debug_identity;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const fields = ["open_id", "user_id", "union_id", "name", "department", "tenant_key", "locale"];
  if (Object.keys(value).length !== fields.length || !fields.every((field) => Object.hasOwn(value, field))) return null;
  const identity = Object.fromEntries(fields.map((field) => [field, debugValue(value[field])]));
  if (Object.values(identity).some((field) => field !== null && field.length > 512)) return null;
  return identity;
}

export function debugDetails(payload) {
  const value = payload?.debug_details;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const valid = (item, depth = 0) => {
    if (item === null || typeof item === "boolean" || typeof item === "number") return true;
    if (typeof item === "string") return item.length <= 512;
    if (depth >= 8 || typeof item !== "object") return false;
    if (Array.isArray(item)) return item.length <= 20 && item.every((child) => valid(child, depth + 1));
    const entries = Object.entries(item);
    return entries.length <= 40 && entries.every(([key, child]) => key.length <= 80 && valid(child, depth + 1));
  };
  return valid(value) ? value : null;
}

export function callbackPage({ delivered, state }) {
  const message = delivered
    ? "正在确认你的开通信息，请勿关闭此页面。"
    : "开通连接已失效。请返回飞书，重新点击“开始使用”。";
  const bridgeScript = delivered ? `<script>
    const status = document.querySelector('#status');
    const result = new URL('/oauth/result', location.origin);
    result.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    result.searchParams.set('state', ${JSON.stringify(state)});
    const socket = new WebSocket(result);
    socket.onmessage = ({data}) => {
      const outcome = JSON.parse(data);
      status.textContent = outcome.status === 'identity_confirmed'
        ? '身份已确认，灵犀正在继续开通。'
        : '本次开通未完成，请返回飞书重新开始。';
      if (outcome.debug_identity) {
        const debug = document.querySelector('#debug');
        const fields = ['open_id', 'user_id', 'union_id', 'name', 'department', 'tenant_key', 'locale'];
        debug.textContent = fields.map((field) => field + ': ' + (outcome.debug_identity[field] || '（未返回）')).join('\\n');
        document.querySelector('#debug-section').hidden = false;
      }
      if (outcome.debug_details) {
        document.querySelector('#debug-details').textContent = JSON.stringify(outcome.debug_details, null, 2);
        document.querySelector('#debug-details-section').hidden = false;
      }
      socket.close();
    };
    socket.onerror = () => { status.textContent = '正在确认开通信息，请稍候或返回飞书查看进度。'; };
  </script>` : "";
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>灵犀开通</title><body><p id="status">${message}</p><section id="debug-section" hidden><p><strong>仅供 Bot-Test 调试：请勿保存、转发或截图其中的身份资料。</strong></p><pre id="debug"></pre></section><section id="debug-details-section" hidden><h2>资料可得性报告</h2><pre id="debug-details"></pre></section><script>history.replaceState(null, '', location.pathname)</script>${bridgeScript}</body></html>`;
}
