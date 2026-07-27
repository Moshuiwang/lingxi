import assert from "node:assert/strict";
import test from "node:test";

import { callbackPage, callbackPayload, hasValidOAuthCallback } from "../src/protocol.js";

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
