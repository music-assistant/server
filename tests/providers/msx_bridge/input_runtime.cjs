// Exercise the shipped TVX URL/service/delay implementation at its transport boundary.
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let handler, busy = false, result = null;
const pending = [], timers = new Map(); let sequence = 0;
const context = {
    console: {log() {}, warn() {}}, navigator: {userAgent: 'MSX test'}, document: {},
    location: {protocol: 'http:', href: 'http://ma:8099/msx/input.html', search: ''},
    addEventListener() {}, parent: {postMessage() {}}, frames: [],
    setTimeout: (fn, delay) => {let id = ++sequence; timers.set(id, {fn, delay}); return id;},
    clearTimeout: id => timers.delete(id), setInterval: () => 1, clearInterval() {},
};
context.window = context;
vm.createContext(context);
vm.runInContext(input.library, context);
context.TVXPluginTools.onReady = fn => fn();
context.TVXInteractionPlugin.setupHandler = h => {handler = h;};
context.TVXInteractionPlugin.init = () => {};
context.TVXInteractionPlugin.requestData = (id, cb) => cb({info: {id: 'pixel', application: {name: 'Media Station X', version: '0.1.165'}}});
context.TVXInteractionPlugin.executeAction = () => {};
context.TVXInteractionPlugin.startLoading = () => {busy = true;};
context.TVXInteractionPlugin.stopLoading = () => {busy = false;};
context.TVXInteractionPlugin.warn = context.TVXInteractionPlugin.error = () => {};
context.TVXAjaxService = {executeRequest(method, url, data, callbacks) {pending.push({url, callbacks}); return true;}};
vm.runInContext(input.script, context);
handler.ready();
const dataId = 'http://ma:8099/msx/search-input.json?q={INPUT}|search:3|en|Search';
handler.handleRequest(dataId, null, () => {});
function flush() {for (const [id, timer] of [...timers]) {if (timer.delay === 3000) {timers.delete(id); timer.fn();}}}
function type(text) {for (const c of text) handler.handleData({message: 'input:' + c}); flush();}
function response(index, title) {pending[index].callbacks.success({template: {type: 'list'}, items: [{title}], compress: true});}
if (input.scenario === 'cancel') {
    type('abc');
    handler.handleData({message: 'control:back'});
    if (busy) throw new Error('Cancelled short query left native loading active');
} else if (input.scenario === 'stale') {
    type('abc');
    handler.handleData({message: 'control:back'});
    type('c');
    response(0, 'old');
    if (!busy) throw new Error('Old same-query response completed the new request');
    response(1, 'new');
    handler.handleRequest(dataId, null, data => {result = data;});
    if (busy || !JSON.stringify(result).includes('new')) throw new Error('Current search result not displayed');
} else if (input.scenario === 'timeout') {
    type('abc');
    for (const [id, timer] of [...timers]) {timers.delete(id); timer.fn();}
    if (busy) throw new Error('Timeout left loading active');
    response(0, 'old');
    if (busy) throw new Error('Late timeout response revived loading');
} else if (input.scenario === 'error') {
    type('abc');
    pending[0].callbacks.error('Network failure');
    if (busy) throw new Error('Error left loading active');
    type('d');
    response(1, 'retry');
    if (busy) throw new Error('Retry left loading active');
} else {
    type(input.query);
    if (pending.length !== 1) throw new Error('Debounce failed');
    if (new URL(pending[0].url).searchParams.get('q') !== input.query) throw new Error('Query encoding changed');
    response(0, 'found');
    if (busy) throw new Error('Success did not finish loading');
}
process.stdout.write('ok');
