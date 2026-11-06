'use strict';
'require view';
'require form';
'require uci';
'require traffic-stats as stats';
'require tools.widgets as widgets';

function duration(value) {
	var match = /^([0-9]+)\s*([smh]?)$/i.exec(String(value).trim());
	return match ? Number(match[1]) * ({ s: 1, m: 60, h: 3600 }[match[2].toLowerCase()] || 1) : NaN;
}

return view.extend({
	load: function() {
		return Promise.all([
			uci.load('traffic-stats'),
			stats.containers().catch(function(error) { return { load_error: error }; })
		]);
	},
	render: function(data) {
		stats.loadStyle();
		var map = new form.Map('traffic-stats');
		map.tabbed = true;
		var SettingsSection = form.NamedSection.extend({
			renderContents: function(data) {
				var node = this.super('renderContents', arguments);
				node.setAttribute('data-tab', this.settingsTab);
				node.style.paddingBlock = '16px';
				var content = node.querySelector('[data-section-id]');
				if (content) content.id += '-' + this.settingsTab;
				return node;
			}
		});
		function settingsSection(tab, title) {
			var section = map.section(SettingsSection, 'main', 'traffic_stats', title);
			section.settingsTab = tab;
			section.hidetitle = true;
			section.addremove = false;
			return section;
		}
		var general = settingsSection('general', _("General settings"));
		var advanced = settingsSection('advanced', _("Advanced settings"));
		var enabled = general.option(form.Flag, 'enabled', _("Enable collection"));
		enabled.default = '0';
		enabled.rmempty = false;
		function deviceValues(value) {
			return Array.isArray(value) ? value : value ? [value] : [];
		}
		function deviceOption(name, legacy, label, description, fallback) {
			var option = general.option(widgets.DeviceSelect, name, label, description);
			option.multiple = true;
			option.noaliases = true;
			option.nocreate = false;
			option.rmempty = true;
			option.forcewrite = true;
			option.cfgvalue = function(sectionId) {
				var value = uci.get('traffic-stats', sectionId, name);
				if (value == null) value = uci.get('traffic-stats', sectionId, legacy);
				return deviceValues(value == null ? fallback : value);
			};
			option.write = function(sectionId, value) {
				var values = deviceValues(value);
				uci.set('traffic-stats', sectionId, name, values.length ? values : '');
				uci.unset('traffic-stats', sectionId, legacy);
			};
			option.remove = function(sectionId) { this.write(sectionId, []); };
			return option;
		}
		var lan = deviceOption('lan_devices', 'lan_device', _("LAN network devices"),
			_("Select multiple devices or enter names, e.g. br-lan. Use device names, not logical interfaces."), 'br-lan');
		var wan = deviceOption('wan_devices', 'wan_device', _("WAN network devices"),
			_("Select multiple devices or enter names, e.g. eth0, pppoe-wan. Must differ from LAN devices."), '');
		function interfaceValidation(other) {
			return function(sectionId, value) {
				var selected = deviceValues(this.formvalue(sectionId));
				var values = value === selected.join(' ') ? selected : deviceValues(value);
				var peers = deviceValues(other.formvalue(sectionId));
				if (!values.length)
					return enabled.formvalue(sectionId) !== '1' || _("Select at least one network device when collection is enabled.");
				if (values.some(function(device) { return !/^[A-Za-z0-9_.-]{1,15}$/.test(device); }))
					return _("Device names must be 1-15 letters, digits, underscores, dots or hyphens.");
				return !values.some(function(device) { return peers.indexOf(device) !== -1; }) || _("LAN and WAN network devices must not overlap.");
			};
		}
		lan.validate = interfaceValidation(wan);
		wan.validate = interfaceValidation(lan);
		var containerData = data[1] || {}, containers = containerData.containers;
		var containersAvailable = !containerData.load_error && Array.isArray(containers);
		containers = containersAvailable ? containers : [];
		var excluded = containerData.excluded_names || [];
		var configured = uci.get('traffic-stats', 'main', 'proxy_containers');
		var legacyIPs = deviceValues(uci.get('traffic-stats', 'main', 'proxy_ips'));
		var selected = configured == null ? containers.filter(function(container) {
			return (container.addresses || []).some(function(address) { return legacyIPs.indexOf(address) !== -1; });
		}).map(function(container) { return container.name; }) : deviceValues(configured);
		var unsupported = selected.filter(function(name) { return excluded.indexOf(name) !== -1; });
		selected = selected.filter(function(name) { return excluded.indexOf(name) === -1; });
		var description = _("Select multiple containers; addresses update automatically. Host mode is excluded. Only client-side traffic is counted to avoid duplicates.");
		if (!containersAvailable)
			description = _("Could not load containers. Check Docker and refresh. The current configuration is preserved and cannot be edited yet.");
		else if (unsupported.length)
			description += ' ' + _("Containers using unsupported network modes will be removed on save.");
		if (containersAvailable && configured == null && legacyIPs.some(function(address) {
			return !containers.some(function(container) { return (container.addresses || []).indexOf(address) !== -1; });
		}))
			description += ' ' + _("Some old IP addresses have no matching container. Select again; old IP settings will be removed on save.");
		var proxies = advanced.option(form.MultiValue, 'proxy_containers', _("Proxy containers"), description);
		proxies.create = false;
		proxies.rmempty = true;
		proxies.forcewrite = true;
		proxies.readonly = !containersAvailable;
		proxies.placeholder = containers.length || selected.length ? _("Select proxy containers") : _("No containers available");
		var known = Object.create(null);
		containers.forEach(function(container) {
			if (excluded.indexOf(container.name) !== -1) return;
			known[container.name] = true;
			var label = container.state === 'running' ? container.name : _('%s (not running)').format(container.name);
			proxies.value(container.name, E('span', {}, [label, ' - ', E('span', { 'class': 'ts-monospace' },
				[(container.addresses || []).join(', ') || _("No address")])]));
		});
		selected.forEach(function(name) {
			if (!known[name]) {
				known[name] = true;
				proxies.value(name, E('span', {}, [_('%s (currently unavailable)').format(name)]));
			}
		});
		proxies.cfgvalue = function() { return selected; };
		proxies.write = function(sectionId, value) {
			if (!containersAvailable) return;
			var values = deviceValues(value).filter(function(name) { return known[name] && excluded.indexOf(name) === -1; });
			if (values.length) uci.set('traffic-stats', sectionId, 'proxy_containers', values);
			else uci.unset('traffic-stats', sectionId, 'proxy_containers');
			uci.unset('traffic-stats', sectionId, 'proxy_ips');
		};
		proxies.remove = function(sectionId) { this.write(sectionId, []); };
		var directory = advanced.option(form.Value, 'storage_dir', _("Storage directory"),
			_("Absolute path on persistent storage: traffic.db stores usage, devices stores device names. Mount external storage first."));
		directory.default = '/etc/traffic-stats';
		directory.rmempty = false;
		directory.cfgvalue = function(sectionId) {
			return uci.get('traffic-stats', sectionId, 'storage_dir') ||
				(uci.get('traffic-stats', sectionId, 'database') || '/etc/traffic-stats/traffic.db').replace(/\/[^/]+$/, '') || '/';
		};
		directory.validate = function(sectionId, value) {
			return /^\/[^\x00-\x1f\x7f]*$/.test(value) && value.split('/').every(function(part) { return part !== '.' && part !== '..'; }) ||
				_("Enter an absolute directory path without control characters or . and .. path segments.");
		};
		directory.write = function(sectionId, value) {
			uci.set('traffic-stats', sectionId, 'storage_dir', value.replace(/\/+$/, '') || '/');
		};
		var interval = general.option(form.Value, 'interval', _("Sampling interval (seconds)"), _("Background sampling frequency, independent of the overview refresh interval."));
		interval.default = '5';
		interval.datatype = 'and(uinteger,range(1,3600))';
		interval.rmempty = false;
		var commit = general.option(form.Value, 'commit_interval', _("Database write interval"), _("Use s/m/h; seconds by default. Range: 1s-24h, at least the sampling interval. Unsaved usage stays in memory."));
		commit.default = '1m';
		commit.cfgvalue = function(sectionId) {
			var seconds = Number(uci.get('traffic-stats', sectionId, 'commit_interval') || 60);
			return seconds % 3600 === 0 ? seconds / 3600 + 'h' : seconds % 60 === 0 ? seconds / 60 + 'm' : seconds + 's';
		};
		commit.write = function(sectionId, value) {
			uci.set('traffic-stats', sectionId, 'commit_interval', String(duration(value)));
		};
		commit.rmempty = false;
		commit.validate = function(sectionId, value) {
			var seconds = duration(value);
			if (!Number.isSafeInteger(seconds) || seconds < 1 || seconds > 86400)
				return _("Enter a whole duration from 1s to 24h using s, m or h.");
			return seconds >= Number(interval.formvalue(sectionId)) || _("The database write interval must not be shorter than the sampling interval.");
		};
		var retention = general.option(form.Value, 'retention', _("History retention"), _("Months: 0 keeps history forever; 1-120 includes the current month. Expired months are deleted from details and daily totals."));
		retention.default = '3';
		retention.datatype = 'and(uinteger,range(0,120))';
		retention.rmempty = false;
		retention.cfgvalue = function(sectionId) {
			var value = uci.get('traffic-stats', sectionId, 'retention');
			if (value == null)
				return String(Math.min(120, Math.ceil(Number(uci.get('traffic-stats', sectionId, 'retention_days') || 90) / 30)));
			var match = /^([1-9][0-9]*)\s*(mo|y|个月|月|年)$/i.exec(String(value).trim());
			return match ? String(Number(match[1]) * (/^(y|年)$/i.test(match[2]) ? 12 : 1)) : value;
		};
		retention.write = function(sectionId, value) {
			uci.set('traffic-stats', sectionId, 'retention', String(Number(value)));
			uci.unset('traffic-stats', sectionId, 'retention_days');
		};
		return map.render().then(function(node) {
			node.classList.add('ts-settings');
			return node;
		});
	}
});
