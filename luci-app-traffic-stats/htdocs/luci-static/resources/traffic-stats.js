'use strict';
'require baseclass';
'require rpc';
'require ui';
'require validation';

var packageVersion = '@TRAFFIC_STATS_VERSION@';

var callStatus = rpc.declare({
	object: 'traffic-stats', method: 'status', expect: { '': {} }, reject: true
});
var callHistory = rpc.declare({
	object: 'traffic-stats', method: 'history',
	params: [ 'resolution', 'from', 'to', 'counter', 'limit' ],
	expect: { '': {} }, reject: true
});
var callContainers = rpc.declare({
	object: 'traffic-stats', method: 'containers', expect: { '': {} }, reject: true
});
var callService = rpc.declare({
	object: 'traffic-stats', method: 'service', params: [ 'action' ],
	expect: { '': {} }, reject: true
});

function unwrap(reply) {
	if (reply && reply.ok === true && reply.data)
		return reply.data;
	var error = new Error(reply && reply.message || _("Request failed"));
	error.code = reply && reply.error || 'request_failed';
	throw error;
}

function userError(message) {
	var error = new Error(message);
	error.userMessage = true;
	return error;
}

function errorMessage(error) {
	var messages = {
		not_initialized: _("No history database yet. Enable collection to start recording usage."),
		response_too_large: _("Too much data returned. Use a shorter date range or daily/monthly records."),
		invalid_request: _("Invalid query parameters. Check the dates and query range."),
		invalid_range: _("The date range is too large. Reduce it or use a coarser resolution."),
		range_too_large: _("The date range is too large. Reduce it or use a coarser resolution."),
		runtime_missing: _("No collection status yet. Enable and start the service first."),
		configuration_error: _("Invalid collection settings. Check the interfaces and database path in Settings."),
		invalid_configuration: _("Invalid collection settings. Check the interfaces and database path in Settings."),
		service_disabled: _("Enable collection in Settings and save and apply before starting the service."),
		service_failed: _("The service operation failed. Check collection settings and the system log."),
		database_error: _("Unable to read the history database. Check the storage location and system log."),
		docker_unavailable: _('Unable to read containers. Check the Docker service and try again.'),
		name_conflict: _('This rule has changed. Close and reopen the editor.'),
		name_read_failed: _('Device names could not be read. Check the storage directory and JSON file.'),
		name_save_failed: _('Device names could not be saved. Check the storage directory and permissions.'),
		unknown_method: _('This operation is not supported. Refresh the page after upgrading the plugin.'),
		history_unavailable: _("Unable to read the history database. Check the storage location and system log.")
	};
	return error && error.userMessage ? error.message : messages[error && error.code] ||
		_('The request could not be completed. Please try again or check the system log.');
}

function integer(value) {
	var text = String(value == null ? '0' : value);
	if (!/^[0-9]+$/.test(text))
		throw userError(_("Invalid traffic value"));
	return BigInt(text);
}

function scalar(value, suffix) {
	if (!Number.isFinite(value) || value < 0)
		return _("Unavailable");
	var units = [ 'B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB' ], index = 0;
	while (value >= 1024 && index < units.length - 1) {
		value /= 1024;
		index++;
	}
	return value.toFixed(index ? (value >= 100 ? 0 : 1) : 0) + ' ' + units[index] + (suffix || '');
}

function bytes(value) {
	return scalar(Number(integer(value)), '');
}

function rate(value) {
	return typeof value === 'number' ? scalar(value, '/s') : _("Unavailable");
}

function time(value) {
	if (!value || !Number.isFinite(Number(value)))
		return _("None yet");
	return new Date(Number(value)).toLocaleString(document.documentElement.lang || 'en');
}

function quality(flags) {
	flags = Number(flags) || 0;
	var labels = [];
	if (flags & 1) labels.push(_("Uncertain timing"));
	if (flags & 2) labels.push(_("System time changed"));
	if (flags & 4) labels.push(_("Collection interrupted"));
	if (flags & 8) labels.push(_("Time zone changed"));
	if (flags & 16) labels.push(_("Recovered after restart"));
	return labels.join(' / ') || _("Normal");
}

var nameList = rpc.declare({ object: 'traffic-stats', method: 'name_list', expect: { '': {} }, reject: true });
var nameSave = rpc.declare({ object: 'traffic-stats', method: 'name_save', params: ['action', 'section', 'name', 'mac', 'ip', 'revision'], expect: { '': {} }, reject: true });

function displayName(device) {
	return device.custom_name || device.hostname || device.mac || device.id;
}

function canonicalIP(value) {
	var ipv4 = validation.parseIPv4(value);
	if (ipv4)
		return ipv4.join('.');
	var ipv6 = validation.parseIPv6(value);
	return ipv6 ? ipv6.map(function(word) { return word.toString(16); }).join(':') : null;
}

async function editDeviceName(device, onSaved) {
	var snapshot, sections = {};
	try {
		snapshot = unwrap(await nameList());
		snapshot.rules.forEach(function(item) { sections[item.section] = item; });
	} catch (error) {
		ui.addNotification(null, E('p', {}, [_('Unable to read name settings. %s').format(errorMessage(error))]), 'error');
		return;
	}
	var writable = L.hasViewPermission(), busy = false;
	var rules = snapshot.rules.map(function(item) { return item.section; });
	var rule = E('select', { 'class': 'ts-monospace', 'aria-label': _("Name rule") }, [ E('option', { value: '' }, _("New rule")) ].concat(rules.map(function(id) {
		var item = sections[id];
		return E('option', { value: id }, [(item.name || _("Unnamed")) + ' | ' +
			[item.mac ? 'MAC ' + item.mac : '', item.ip ? 'IP ' + item.ip : ''].filter(Boolean).join(' + ')]);
	})));
	var name = E('input', { type: 'text', maxlength: 128, 'aria-label': _("Custom name"), placeholder: _("Example: My laptop"), disabled: writable ? null : true });
	var mac = E('input', { type: 'text', 'class': 'ts-monospace', autocapitalize: 'none', spellcheck: 'false', 'aria-label': _("Match MAC"), placeholder: _("Optional"), disabled: writable ? null : true });
	var ip = E('input', { type: 'text', 'class': 'ts-monospace', autocapitalize: 'none', spellcheck: 'false', 'aria-label': _("Match IP"), placeholder: _("IPv4 or IPv6, optional"), disabled: writable ? null : true });
	var errorBox = E('p', { 'class': 'ts-name-error', role: 'alert' });
	var save = E('button', { type: 'submit', 'class': 'cbi-button cbi-button-action', disabled: writable ? null : true }, _("Save name"));
	var remove = E('button', { type: 'button', 'class': 'cbi-button cbi-button-negative', click: function() { return persist(true); } }, _("Delete rule"));
	var cancel = E('button', { type: 'button', 'class': 'cbi-button', click: ui.hideModal }, _("Close"));

	function selected() {
		var entry = sections[rule.value];
		name.value = entry && typeof entry.name === 'string' ? entry.name : device.custom_name || device.hostname || '';
		mac.value = entry && typeof entry.mac === 'string' ? entry.mac : entry ? '' : device.mac || '';
		ip.value = entry && typeof entry.ip === 'string' ? entry.ip : '';
		remove.disabled = !writable || !entry;
		errorBox.textContent = '';
	}
	rule.addEventListener('change', selected);
	var addresses = (device.addresses || []).map(canonicalIP), specificity = -1;
	rule.value = '';
	snapshot.rules.forEach(function(item) {
		var score = (item.mac ? 2 : 0) + (item.ip ? 1 : 0);
		if (score > specificity && (!item.mac || item.mac === (device.mac || '').toLowerCase()) &&
			(!item.ip || addresses.indexOf(canonicalIP(item.ip)) !== -1)) {
			rule.value = item.section;
			specificity = score;
		}
	});
	selected();

	async function persist(deleting) {
		if (busy || !writable)
			return;
		var id = rule.value;
		var values = { name: name.value.trim(), mac: mac.value.trim().toLowerCase(), ip: ip.value.trim() };
		if (!deleting) {
			var message = !values.name || Array.from(values.name).length > 128 || /[\x00-\x1f\x7f]/.test(values.name) ? _("Enter a name of 1-128 characters without control characters.") :
				!values.mac && !values.ip ? _("Enter at least one MAC or IP address.") :
				values.mac && !/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/.test(values.mac) ? _("Enter a valid MAC address.") :
				values.ip && !canonicalIP(values.ip) ? _("Enter one IPv4 or IPv6 address without a subnet or interface suffix.") : '';
			if (message) { errorBox.textContent = message; return; }
			if (values.ip)
				values.ip = canonicalIP(values.ip);
		}
		busy = true;
		[save,remove,cancel,rule,name,mac,ip].forEach(function(node) { node.disabled = true; });
		errorBox.textContent = '';
		try {
			if (deleting && !id) return;
			unwrap(await nameSave(deleting ? 'delete' : 'save', id, values.name, values.mac, values.ip, snapshot.revision));
		} catch (error) {
			errorBox.textContent = _('Unable to save. %s').format(errorMessage(error));
			return;
		} finally {
			busy = false;
			[save,cancel,rule,name,mac,ip].forEach(function(node) { node.disabled = !writable && node !== cancel && node !== rule; });
			remove.disabled = !writable || !rule.value;
		}
		ui.hideModal();
		await onSaved();
	}

	function field(label, input) { return E('label', { 'class': 'ts-name-field' }, [ E('span', {}, label), input ]); }
	ui.showModal(_("Device name"), [ E('form', {
		'class': 'ts-name-editor', submit: function(event) { event.preventDefault(); return persist(false); }
	}, [
		E('p', { 'class': 'ts-name-device' }, [ device.mac || device.id,
			E('br'), (device.addresses || []).join(' / ') || _("No current IP address") ]),
		field(_("Rule"), rule), field(_("Custom name"), name), field(_("Match MAC"), mac), field(_("Match IP"), ip),
		E('p', { 'class': 'ts-name-help' }, _("Enter MAC or IP; if both are set, both must match. Priority: MAC + IP > MAC > IP, then configuration order.")),
		E('p', { 'class': 'ts-name-help' }, _("Names are display labels only. Deleting a rule restores another matching rule or the hostname.")),
		errorBox,
		E('div', { 'class': 'ts-name-actions' }, [
			E('a', { href: L.url('admin/services/traffic-stats/history') + '?device=' + encodeURIComponent(device.id) }, _("View device history")),
			E('div', {}, [remove,cancel,save])
		])
	]) ]);
	if (writable && !window.matchMedia('(pointer: coarse)').matches) name.focus();
}

function deviceList(status) {
	var devices = {};
	(status.known_devices || []).concat(status.devices || []).forEach(function(device) {
		if (device && /^[0-9a-f]{12}$/.test(device.id || '')) {
			var previous = devices[device.id] || {};
			devices[device.id] = Object.assign({}, previous, device, {
				hostname: device.hostname || previous.hostname || ''
			});
		}
	});
	return Object.keys(devices).map(function(id) { return devices[id]; }).sort(function(a, b) {
		return displayName(a).localeCompare(displayName(b));
	});
}

return baseclass.extend({
	footer: function() {
		var version = packageVersion.charAt(0) === '@' ? 'dev' : 'v' + packageVersion;
		return E('small', { 'class': 'ts-footer' }, [E('a', {
			href: 'https://github.com/w9315273/openwrt-traffic-stats', target: '_blank', rel: 'noopener noreferrer'
		}, [E('span', { 'class': 'ts-footer-github', 'aria-hidden': 'true' }), 'Traffic Stats ' + version])]);
	},
	status: function() { return callStatus().then(unwrap); },
	containers: function() { return callContainers().then(unwrap); },
	history: function(resolution, from, to, counter) {
		return callHistory(resolution, from, to, counter, 1000).then(unwrap);
	},
	service: function(action) { return callService(action).then(unwrap); },
	errorMessage: errorMessage,
	integer: integer,
	bytes: bytes,
	rate: rate,
	time: time,
	quality: quality,
	deviceList: deviceList,
	displayName: displayName,
	editDeviceName: editDeviceName,
	loadStyle: function() {
		if (!document.getElementById('traffic-stats-style'))
			document.head.appendChild(E('link', {
				id: 'traffic-stats-style', rel: 'stylesheet', href: L.resource('traffic-stats.css')
			}));
	},
	url: function(page) { return L.url('admin/services/traffic-stats/' + page); },
	notice: function(message, kind) {
		return E('div', { 'class': 'ts-notice ts-' + (kind || 'info'), role: 'status' }, [message]);
	},
	empty: function(title, message) {
		return E('div', { 'class': 'ts-empty' }, [
			E('strong', {}, [title]), E('p', { 'class': 'ts-muted' }, [message])
		]);
	},
	replace: function(node, children) {
		while (node.firstChild)
			node.removeChild(node.firstChild);
		(Array.isArray(children) ? children : [ children ]).forEach(function(child) {
			if (child != null)
				node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
		});
	}
});
