'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const html = fs.readFileSync('static/index.html', 'utf8');
const app = fs.readFileSync('static/app.js', 'utf8');

test('online overview does not render the removed trip/stay module', () => {
  const overview = html.slice(html.indexOf('id="overviewView"'), html.indexOf('id="signalsView"'));
  assert.doesNotMatch(overview, /行程与停留/);
  assert.doesNotMatch(overview, /id="segments"/);
  assert.doesNotMatch(app, /\$\('segments'\)\.innerHTML/);
});

test('offline analysis keeps its independent segment evidence panel', () => {
  assert.match(html, /id="offlineSegments"/);
  assert.match(app, /\$\('offlineSegments'\)\.innerHTML/);
});
