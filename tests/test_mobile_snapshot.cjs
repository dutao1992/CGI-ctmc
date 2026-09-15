'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const html = fs.readFileSync('static/index.html', 'utf8');
const css = fs.readFileSync('static/style.css', 'utf8');
const app = fs.readFileSync('static/app.js', 'utf8');

test('mobile overview keeps the CGI device snapshot visible', () => {
  assert.match(html, /class="panel snapshot-panel"/);
  assert.match(css, /@media\(max-width:700px\)\{\.snapshot-panel\{display:block\}/);
  assert.match(app, /\$\('deviceSnapshot'\)\.innerHTML/);
});
