import { BRIDGE_PATH, CALLBACK_PATH, DELIVERY_PATH, RESULT_PATH, callbackPage, callbackPayload, hasValidOAuthCallback, isOpaqueState } from "./protocol.js";

const BRIDGE_NAME = "bot-test";
const OUTCOME_TTL_MS = 5 * 60 * 1000;

const deliveryKey = (state) => `delivery:${state}`;
const outcomeKey = (state) => `outcome:${state}`;

function unauthorized() {
  return new Response("unauthorized", { status: 401, headers: { "WWW-Authenticate": "Bearer" } });
}

export function callbackResponse(delivered, state) {
  return new Response(callbackPage({ delivered, state }), {
    status: delivered ? 200 : 409,
    headers: {
      "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'",
      "Content-Type": "text/html; charset=utf-8",
      "Referrer-Policy": "no-referrer"
    }
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === BRIDGE_PATH) {
      if (request.method !== "GET" || request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
        return new Response("WebSocket upgrade required", { status: 426 });
      }
      if (request.headers.get("Authorization") !== `Bearer ${env.STAGE_BRIDGE_TOKEN}`) {
        return unauthorized();
      }
      return env.OAUTH_BRIDGE.getByName(BRIDGE_NAME).fetch(request);
    }

    if (url.pathname === CALLBACK_PATH) {
      if (request.method !== "GET" || !hasValidOAuthCallback(url)) {
        return new Response("invalid authorization result", { status: 400, headers: { "Cache-Control": "no-store" } });
      }
      const result = await env.OAUTH_BRIDGE.getByName(BRIDGE_NAME).fetch(
        new Request("https://oauth-bridge.internal" + DELIVERY_PATH, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(callbackPayload(url))
        })
      );
      return callbackResponse(result.ok, url.searchParams.get("state"));
    }

    if (url.pathname === RESULT_PATH) {
      const state = url.searchParams.get("state");
      if (request.method !== "GET" || request.headers.get("Upgrade")?.toLowerCase() !== "websocket" || !isOpaqueState(state)) {
        return new Response("invalid result connection", { status: 400 });
      }
      return env.OAUTH_BRIDGE.getByName(BRIDGE_NAME).fetch(request);
    }

    return new Response("not found", { status: 404 });
  }
};

export class OAuthBridge {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === BRIDGE_PATH) {
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      for (const socket of this.state.getWebSockets("stage")) {
        socket.close(1000, "replaced by current biai-stage bridge");
      }
      this.state.acceptWebSocket(server, ["stage"]);
      server.serializeAttachment({ role: "stage" });
      return new Response(null, { status: 101, webSocket: client });
    }

    if (url.pathname === RESULT_PATH) {
      const state = new URL(request.url).searchParams.get("state");
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      for (const socket of this.state.getWebSockets(`browser:${state}`)) {
        socket.close(1000, "replaced by current browser");
      }
      this.state.acceptWebSocket(server, [`browser:${state}`]);
      server.serializeAttachment({ role: "browser", state });
      const stored = await this.readOutcome(state);
      if (stored) {
        server.send(JSON.stringify({ type: "oauth_result", status: stored.status }));
        await this.state.storage.delete(outcomeKey(state));
      }
      return new Response(null, { status: 101, webSocket: client });
    }

    if (url.pathname === DELIVERY_PATH && request.method === "POST") {
      const payload = await request.json();
      if (!payload || !isOpaqueState(payload.state) || !["oauth_code", "oauth_cancelled"].includes(payload.type)) {
        return new Response("invalid delivery", { status: 400 });
      }
      if (await this.readFresh(deliveryKey(payload.state))) {
        return new Response("authorization result already delivered", { status: 409 });
      }
      const sockets = this.state.getWebSockets("stage");
      if (sockets.length !== 1) {
        return new Response("bridge unavailable", { status: 409 });
      }
      try {
        sockets[0].send(JSON.stringify(payload));
      } catch {
        return new Response("bridge unavailable", { status: 409 });
      }
      await this.state.storage.put(deliveryKey(payload.state), { expiresAt: Date.now() + OUTCOME_TTL_MS });
      return new Response(null, { status: 204 });
    }

    return new Response("not found", { status: 404 });
  }

  async readFresh(key) {
    const value = await this.state.storage.get(key);
    if (!value || value.expiresAt <= Date.now()) {
      if (value) await this.state.storage.delete(key);
      return null;
    }
    return value;
  }

  async readOutcome(state) {
    return this.readFresh(outcomeKey(state));
  }

  async webSocketMessage(socket, message) {
    const attachment = socket.deserializeAttachment();
    if (attachment?.role !== "stage" || typeof message !== "string") {
      return;
    }
    try {
      const outcome = JSON.parse(message);
      if (outcome.type !== "oauth_result" || !isOpaqueState(outcome.state) || !["identity_confirmed", "retry"].includes(outcome.status)) {
        return;
      }
      const payload = JSON.stringify({ type: "oauth_result", status: outcome.status });
      const browsers = this.state.getWebSockets(`browser:${outcome.state}`);
      if (browsers.length === 0) {
        await this.state.storage.put(outcomeKey(outcome.state), { status: outcome.status, expiresAt: Date.now() + OUTCOME_TTL_MS });
      }
      for (const browser of browsers) {
        browser.send(payload);
      }
    } catch {
      // 未识别消息一律忽略，避免 Worker 成为反向业务入口。
    }
  }

  webSocketClose(socket, code, reason) {
    socket.close(code, reason);
  }
}
