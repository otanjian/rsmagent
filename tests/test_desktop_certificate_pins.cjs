const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../desktop/node_modules/typescript');
const source = fs.readFileSync(require('node:path').join(__dirname, '../desktop/src/main/remote/certificate-pins.ts'), 'utf8');
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText,
    {exports: exportsObject, URL});
const {parseCertificatePins, acceptsPinnedCertificate} = exportsObject;

test('a saved pin only accepts the exact HTTPS origin and certificate', () => {
    const fingerprint = '12:'.repeat(31) + '12';
    const pins = parseCertificatePins(JSON.stringify([{origin:'https://sap.example:44300', fingerprint}]));
    assert.ok(acceptsPinnedCertificate(pins, 'https://sap.example:44300/sap/login', fingerprint));
    for (const url of ['https://sap.example/sap', 'https://other.example:44300/sap', 'http://sap.example:44300/', 'https://user@sap.example:44300/']) {
        assert.equal(acceptsPinnedCertificate(pins, url, fingerprint), false);
    }
    assert.equal(acceptsPinnedCertificate(pins, 'https://sap.example:44300/', '34'.repeat(32)), false);
    assert.equal(acceptsPinnedCertificate([], 'https://sap.example:44300/', fingerprint), false);
});

test('malformed pins never change certificate validation', () => {
    for (const raw of ['', '{}', '[null]', '[{}]', 'not json']) assert.equal(parseCertificatePins(raw).length, 0);
    for (const origin of ['http://sap.example', 'https://sap.example/path', 'https://user:pass@sap.example']) {
        assert.equal(parseCertificatePins(JSON.stringify([{origin, fingerprint:'12'.repeat(32)}])).length, 0);
    }
});
