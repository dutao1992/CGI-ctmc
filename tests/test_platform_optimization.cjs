'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const app = fs.readFileSync('static/app.js', 'utf8');
const trace = fs.readFileSync('analysis/platform-optimization-20260906/trace/static/app.js', 'utf8');
const portal = fs.readFileSync('analysis/platform-optimization-20260906/portal/frontend/src/App.tsx', 'utf8');
const machine = fs.readFileSync('analysis/platform-optimization-20260906/portal/machine-watch-dist/app.js', 'utf8');

test('vehicle refresh uses a revision and stops on an auth failure', () => {
  assert.match(app, /lastQueriedRevision/);
  assert.match(app, /data\.data_revision/);
  assert.match(app, /state\.authFailure/);
  assert.match(app, /登录已过期，请返回质检平台重新登录/);
});

test('trace boot uses a compact payload and defers details', () => {
  assert.match(trace, /bootstrap\?compact=1/);
  assert.match(trace, /ensureWorkspaceDetails/);
});

test('portal keeps permitted links usable while health is unknown', () => {
  assert.match(portal, /"unknown"/);
  assert.match(portal, /module\.href && canAccess/);
});

test('machine snapshot is cacheable and has an honest accessible label', () => {
  assert.match(machine, /cache: "no-cache"/);
  assert.match(machine, /数据快照：线上页面展示最近一次校验通过的数据/);
});
