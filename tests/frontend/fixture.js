'use strict';

// 仅替换 LuCI 框架边界和 RPC, 共享格式化逻辑及视图均加载项目源码.
window._ = text => text;
String.prototype.format = function(...values) {
    let index = 0;
    return this.replace(/%s/g, () => values[index++]);
};
window.E = (tag, attributes, children) => {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes || {})) {
        if (typeof value === 'function') element.addEventListener(key, value);
        else if (key === 'hidden') element.hidden = value;
        else element.setAttribute(key, value);
    }
    for (const child of Array.isArray(children) ? children : [children]) {
        if (child != null) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
};
window.baseclass = window.view = { extend: definition => definition };
window.poll = { add: callback => { window.refresh = callback; }, remove: () => {} };
window.ui = {};
window.validation = {};
window.L = {
    resource: name => '/luci-static/resources/' + name,
    url: path => '/cgi-bin/luci/' + path,
    hasViewPermission: () => true
};
window.rpc = {
    declare: ({ method }) => async () => {
        if (method === 'status') return { ok: true, data: window.statusData };
        throw new Error('Unexpected RPC method: ' + method);
    }
};
window.statusData = {
    configuration: { enabled: true, lan_devices: ['br-lan', 'br-lan2', 'iot'] },
    table_exists: true, table_owned: true, running: true, sampling_ok: true,
    local_date: '2026-11-08',
    monthly: { month: '2026-11', counters: { total_up: '123', total_down: '456' } },
    devices: [
        { id: '000000000001', hostname: 'Main', interfaces: ['br-lan'], up: {}, down: {} },
        { id: '000000000002', hostname: 'Guest', interfaces: ['br-lan2'], up: {}, down: {} },
        { id: '000000000003', hostname: 'Sensor', interfaces: ['iot'], up: {}, down: {} },
        { id: '000000000004', hostname: 'Router', local: true, interfaces: ['br-lan', 'iot'], up: {}, down: {} }
    ],
    known_devices: [{ id: '000000000005', hostname: 'Saved' }]
};
