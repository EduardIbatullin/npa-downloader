const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function page() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      listeners: {}, value: '', textContent: '', disabled: true,
      classList: { add() {}, remove() {} },
      addEventListener(type, handler) { this.listeners[type] = handler; },
      contains(target) { return target === this; },
    });
    return elements.get(id);
  }
  const document = element('document');
  document.querySelector = element;
  const calls = [];
  const context = vm.createContext({
    document, console, FormData, setTimeout: () => 0, clearTimeout() {},
    fetch: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => url === '/api/health' ? {} : {
        job_id: 'job1', status: 'done', elapsed_seconds: 1,
      } };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/npa_pipeline/static/app.js'), 'utf8'), context);
  return { element, document, calls };
}

for (const method of ['picker', 'drop', 'paste']) {
  test(`${method}: selects PDF and submits it to OCR`, async () => {
    const { element, document, calls } = page();
    const file = new File(['pdf bytes'], 'scan.pdf', { type: 'application/pdf' });
    let prevented = false;
    const event = {
      preventDefault() { prevented = true; },
      target: method === 'drop' ? element('#pdf-drop-zone') : { files: [file] },
      dataTransfer: { files: [file] }, clipboardData: { files: [file] },
    };
    if (method === 'picker') element('#pdf-file').listeners.change(event);
    else document.listeners[method](event);
    assert.equal(element('#btn-upload').disabled, false);
    assert.match(element('#pdf-selected').textContent, /scan.pdf/);
    if (method !== 'picker') assert.equal(prevented, true);
    await element('#upload-form').listeners.submit({ preventDefault() {} });
    const upload = calls.find(call => call.url === '/api/ocr/upload');
    assert.equal(upload.options.body.get('file').name, 'scan.pdf');
    assert.match(element('#upload-status').innerHTML, /\/api\/ocr\/jobs\/job1\/docx/);
  });
}

test('text paste is preserved; invalid files cannot be submitted', () => {
  const { element, document } = page();
  document.listeners.paste({ clipboardData: { files: [] }, preventDefault() { assert.fail('Text paste blocked'); } });
  element('#pdf-file').listeners.change({ target: { files: [new File(['text'], 'file.txt')] } });
  assert.equal(element('#btn-upload').disabled, true);
  element('#pdf-file').listeners.change({ target: { files: [new File([], 'empty.pdf')] } });
  assert.equal(element('#btn-upload').disabled, true);
});
