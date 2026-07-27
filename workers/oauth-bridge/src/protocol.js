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
      socket.close();
    };
    socket.onerror = () => { status.textContent = '正在确认开通信息，请稍候或返回飞书查看进度。'; };
  </script>` : "";
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>灵犀开通</title><body><p id="status">${message}</p><script>history.replaceState(null, '', location.pathname)</script>${bridgeScript}</body></html>`;
}
