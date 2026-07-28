import assert from "node:assert/strict";
import test from "node:test";

import { callbackPage, callbackPayload, debugDetails, debugIdentity, hasValidOAuthCallback } from "../src/protocol.js";
import { callbackResponse } from "../src/worker.js";

const state = "s".repeat(32);

test("only one OAuth outcome and a sufficiently random state are accepted", () => {
  assert.equal(hasValidOAuthCallback(new URL(`https://example.test/oauth/callback?state=${state}&code=one-time`)), true);
  assert.equal(hasValidOAuthCallback(new URL(`https://example.test/oauth/callback?state=${state}&error=access_denied`)), true);
  assert.equal(hasValidOAuthCallback(new URL("https://example.test/oauth/callback?state=short&code=one-time")), false);
  assert.equal(hasValidOAuthCallback(new URL(`https://example.test/oauth/callback?state=${state}&code=x&error=access_denied`)), false);
});

test("the Worker transfers only an opaque state and one-time result", () => {
  assert.deepEqual(callbackPayload(new URL(`https://example.test/oauth/callback?state=${state}&code=one-time`)), { type: "oauth_code", state, code: "one-time" });
  assert.deepEqual(callbackPayload(new URL(`https://example.test/oauth/callback?state=${state}&error=access_denied`)), { type: "oauth_cancelled", state });
});

test("callback page removes the authorization query from browser history", () => {
  assert.match(callbackPage({ delivered: true, state }), /history\.replaceState/);
  assert.match(callbackPage({ delivered: true, state }), /\/oauth\/result/);
  assert.match(callbackPage({ delivered: false, state }), /重新点击/);
});

test("callback page may open its same-origin result notification channel", () => {
  assert.match(callbackResponse(true, state).headers.get("Content-Security-Policy"), /connect-src 'self'/);
});

test("debug identity must have the complete, bounded test-only field set", () => {
  const identity = {
    open_id: "ou_test", user_id: null, union_id: "on_test", name: "测试用户",
    department: null, tenant_key: "tenant", locale: "zh_cn"
  };
  assert.deepEqual(debugIdentity({ debug_identity: identity }), identity);
  assert.equal(debugIdentity({ debug_identity: { open_id: "ou_test" } }), null);
  assert.equal(debugIdentity({ debug_identity: { ...identity, name: "x".repeat(513) } }), null);
  assert.match(callbackPage({ delivered: true, state }), /仅供 Bot-Test 调试/);
});

test("debug report is bounded and keeps nested department paths", () => {
  const report = { 所属部门: [{ id: "od-child", name: "子部门", children: [{ id: "od-parent", name: "父部门", children: [] }] }] };
  assert.deepEqual(debugDetails({ debug_details: report }), report);
  assert.equal(debugDetails({ debug_details: { tooLong: "x".repeat(513) } }), null);
  assert.match(callbackPage({ delivered: true, state }), /资料可得性报告/);
});
