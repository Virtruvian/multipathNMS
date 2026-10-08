const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('live stream follows page access level and uses secure websockets on HTTPS', () => {
  for (const [channel, protocol, expected] of [
    ['live', 'http:', 'ws://nms.example/ws/live'],
    ['admin', 'https:', 'wss://nms.example/ws/admin'],
  ]) {
    let url;
    const context = vm.createContext({
      document: {body: {dataset: {liveChannel: channel}}, getElementById: () => null},
      location: {protocol, host: 'nms.example'}, window: {},
      WebSocket: function(value) {url = value;},
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
    assert.equal(url, expected);
  }
});
