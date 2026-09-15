const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const utilPath = path.join(__dirname, '..', 'public', 'assembly-sq-dashboard', 'dashboard-utils.js');
const context = { module: { exports: {} } };
vm.runInNewContext(fs.readFileSync(utilPath, 'utf8'), context, { filename: utilPath });
const { escapeHtml, normalizeRows, inspectionTaskRate } = context.module.exports;

assert.equal(escapeHtml('<img src=x onerror=1>'), '&lt;img src=x onerror=1&gt;');
assert.equal(inspectionTaskRate(100, 0, 100), 50);
assert.equal(inspectionTaskRate(100, 100, 100), 100);
assert.equal(inspectionTaskRate(200, 200, 100), 100);
assert.equal(inspectionTaskRate(1, 1, 0), 0);
assert.equal(normalizeRows([{ 工序短文本: '閽冲伐(EX21)' }])[0].工序短文本, '钳工(EX21)');
assert.equal(normalizeRows([{ 工序短文本: '闁藉啿浼#(EX21)' }])[0].工序短文本, '钳工(EX21)');

const htmlPath = path.join(__dirname, '..', 'public', 'assembly-sq-dashboard', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');
assert.match(html, /MAX_UPLOAD_BYTES = 5 \* 1024 \* 1024/);
assert.match(html, /MAX_UPLOAD_ROWS = 20000/);
assert.match(html, /inspectionTaskRate\(st\.selfQty, st\.qcQty, st\.reportQty\)/);
assert.match(html, /integrity="sha384-/);

console.log('assembly dashboard tests passed');
