'use strict';
'require view';
'require poll';
'require traffic-stats as stats';

function placeholder() {
	return E('span', { 'class': 'ts-placeholder' }, '--');
}

function rateValue(value) {
	return Number.isFinite(value) && value >= 0 ? stats.rate(value) : placeholder();
}

function metric(label, value, direction) {
	return E('div', { 'class': 'ts-metric ts-metric-' + direction }, [
		E('span', { 'class': 'ts-metric-label' }, [
			E('span', { 'class': 'ts-metric-icon', 'aria-hidden': 'true' }, direction === 'up' ? '↑' : '↓'), label
		]),
		E('strong', {}, value)
	]);
}

function metricGroup(title, items, status) {
	return E('section', { 'class': 'ts-metric-group' }, [
		E('div', { 'class': 'ts-metric-group-header' }, [
			E('h3', { 'class': 'ts-metric-group-title' }, title), status
		].filter(Boolean))
	].concat(items));
}

return view.extend({
	load: function() {
		return stats.status().catch(function(error) { return { load_error: error }; });
	},
	render: function(initial) {
		stats.loadStyle();
		var inFlight = null, latest = initial;
		var sortKey = 'total', sortDescending = true;
		var deviceRows = new Map(), deviceTable = null, emptyMessage = null;
		var selectedLan = '', lanKey = null;
		var lanFilters = E('div', { 'class': 'ts-lan-filters', hidden: true, scroll: updateScrollFade }, [
			E('div', { 'class': 'ts-refresh-options', role: 'group', 'aria-label': _('LAN devices') })
		]);
		var notices = E('div', { 'aria-live': 'polite' });
		var statusPinned = false, statusTouch = false;
		var statusTooltip = E('span', { id: 'ts-collection-status-tooltip', 'class': 'ts-status-tooltip', role: 'tooltip', hidden: true });
		var statusIndicator = E('button', { type: 'button', 'class': 'ts-inline-state',
			'aria-describedby': 'ts-collection-status-tooltip', 'aria-pressed': 'false',
			pointerdown: function(event) { statusTouch = event.pointerType === 'touch' || event.pointerType === 'pen'; },
			click: function(event) {
				if (!statusTouch && event.detail !== 0) return;
				statusPinned = !statusPinned;
				showStatus(statusPinned);
				statusIndicator.setAttribute('aria-pressed', String(statusPinned));
				document[statusPinned ? 'addEventListener' : 'removeEventListener']('pointerdown', dismissStatus);
			},
			focus: function() { showStatus(true); },
			blur: function() { if (!statusPinned) showStatus(false); }
		});
		var statusControl = E('span', { 'class': 'ts-status-control',
			pointerenter: function(event) { if (event.pointerType !== 'touch') showStatus(true); },
			pointerleave: function() { if (!statusPinned) showStatus(false); }
		}, [statusIndicator, statusTooltip]);
		function showStatus(visible) {
			statusTooltip.hidden = !visible;
			document[visible ? 'addEventListener' : 'removeEventListener']('keydown', dismissStatusKey);
		}
		function dismissStatus(event) {
			if (event && statusControl.contains(event.target)) return;
			statusPinned = false;
			statusIndicator.setAttribute('aria-pressed', 'false');
			showStatus(false);
			document.removeEventListener('pointerdown', dismissStatus);
		}
		function dismissStatusKey(event) { if (event.key === 'Escape') dismissStatus(); }
		var metrics = E('div', { 'class': 'ts-metrics' }, [
			metricGroup(_('Current rates'), [
				metric(_('Upload rate'), placeholder(), 'up'), metric(_('Download rate'), placeholder(), 'down')
			], statusControl),
			metricGroup(_('Monthly usage'), [
				metric(_('Monthly upload'), placeholder(), 'up'), metric(_('Monthly download'), placeholder(), 'down')
			])
		]);
		var metricValues = metrics.querySelectorAll('.ts-metric strong');
		var devices = E('div', { 'class': 'ts-table-scroll' });
		var intervals = [ 1, 3, 5, 10 ], refreshInterval = 3;
		try {
			selectedLan = localStorage.getItem('traffic-stats.lan-filter') || '';
			var savedInterval = Number(localStorage.getItem('traffic-stats.refresh-interval'));
			if (intervals.indexOf(savedInterval) !== -1)
				refreshInterval = savedInterval;
		} catch (error) { /* Storage may be unavailable in a private browser session. */ }
		var intervalButtons = intervals.map(function(seconds) {
			return E('button', {
				type: 'button', 'class': 'cbi-button ts-refresh-option',
				'aria-pressed': String(seconds === refreshInterval),
				click: function() {
					if (refreshInterval === seconds)
						return;
					refreshInterval = seconds;
					intervalButtons.forEach(function(button, index) {
						button.setAttribute('aria-pressed', String(intervals[index] === seconds));
					});
					try { localStorage.setItem('traffic-stats.refresh-interval', String(seconds)); }
					catch (error) { /* Polling does not depend on persisted preferences. */ }
					poll.remove(refresh);
					poll.add(refresh, seconds);
					setRefreshAnimation();
				}
			}, [E('span', { 'class': 'ts-refresh-label' }, seconds + 's'), E('span', { 'class': 'ts-refresh-progress', 'aria-hidden': 'true' })]);
		});
		var page = E('div', { 'class': 'ts-page ts-overview' }, [
			E('div', { 'class': 'ts-overview-cards' }, [ metrics ]), notices,
			E('section', { 'class': 'ts-section' }, [
				E('div', { 'class': 'ts-toolbar' }, [
					E('h3', {}, _('Devices')), lanFilters,
					E('div', { 'class': 'ts-list-actions' }, [
						E('div', { 'class': 'ts-refresh', role: 'group', 'aria-label': _('Refresh interval') }, [
							E('span', { 'class': 'ts-muted' }, _('Refresh')),
							E('div', { 'class': 'ts-refresh-options' }, intervalButtons)
						])
					])
				]), devices
			])
		]);

		var unknownRow = null, unknownTouch = false;
		var unknownTooltip = E('div', { id: 'ts-unknown-tooltip', 'class': 'ts-unknown-tooltip', role: 'tooltip', hidden: true }, [
			E('div', { 'class': 'ts-unknown-tooltip-heading' }, [
				E('span', { 'class': 'ts-unknown-info', 'aria-hidden': 'true' }, 'i'),
				E('strong', {}, _('Unattributed traffic'))
			]),
			E('p', {}, _('Included in totals, but not yet matched to a device.')),
			E('p', { 'class': 'ts-unknown-reason' }, _('Device addresses may still be undiscovered or their ownership unclear.')),
			E('div', { 'class': 'ts-unknown-note' }, _('Once identified, new traffic goes to the device; earlier usage stays here.'))
		]);
		page.appendChild(unknownTooltip);
		function hideUnknown() {
			unknownTooltip.hidden = true;
			if (unknownRow) unknownRow.querySelector('button').setAttribute('aria-expanded', 'false');
			unknownRow = null;
			document.removeEventListener('pointerdown', dismissUnknown);
			document.removeEventListener('keydown', unknownKey);
			window.removeEventListener('scroll', hideUnknown, true);
			window.removeEventListener('resize', hideUnknown);
		}
		function dismissUnknown(event) {
			if (!unknownRow || !unknownRow.contains(event.target)) hideUnknown();
		}
		function unknownKey(event) { if (event.key === 'Escape') hideUnknown(); }
		function showUnknown(row, pointer) {
			unknownRow = row;
			unknownTooltip.hidden = false;
			row.querySelector('button').setAttribute('aria-expanded', 'true');
			var rect = row.getBoundingClientRect(), card = row.closest('.ts-section').getBoundingClientRect();
			var minLeft = Math.max(8, card.left + 12), maxRight = Math.min(window.innerWidth - 8, card.right - 12);
			unknownTooltip.style.maxWidth = Math.max(0, maxRight - minLeft) + 'px';
			var box = unknownTooltip.getBoundingClientRect();
			var minTop = Math.max(8, card.top + 12), maxBottom = Math.min(window.innerHeight - 8, card.bottom - 12);
			var anchor = pointer ? pointer.clientX : minLeft + box.width / 2;
			var left = Math.max(minLeft, Math.min(anchor - box.width / 2, maxRight - box.width));
			var below = rect.top - box.height - 12 < minTop && rect.bottom + box.height + 12 <= maxBottom;
			var top = below ? rect.bottom + 12 : rect.top - box.height - 12;
			unknownTooltip.classList.toggle('is-below', below);
			unknownTooltip.style.setProperty('--ts-arrow-left', Math.max(16, Math.min(anchor - left, box.width - 16)) + 'px');
			unknownTooltip.style.left = left + 'px';
			unknownTooltip.style.top = Math.max(minTop, Math.min(top, maxBottom - box.height)) + 'px';
			document.addEventListener('pointerdown', dismissUnknown);
			document.addEventListener('keydown', unknownKey);
			window.addEventListener('scroll', hideUnknown, true);
			window.addEventListener('resize', hideUnknown);
		}
		function toggleUnknown(row) {
			if (!unknownTooltip.hidden) hideUnknown();
			else showUnknown(row);
		}

		function setScrollFade(element) {
			element.classList.toggle('ts-fade-left', element.scrollLeft > 1);
			element.classList.toggle('ts-fade-right', element.scrollWidth - element.clientWidth - element.scrollLeft > 1);
		}
		function updateScrollFade(event) {
			setScrollFade(event.currentTarget);
		}
		var scrollObserver = new ResizeObserver(function(entries) {
			if (!page.isConnected) { scrollObserver.disconnect(); return; }
			entries.forEach(function(entry) {
				setScrollFade(entry.target === lanFilters.firstChild ? lanFilters : entry.target);
			});
		});

		function sortHeader(label, key) {
			var active = sortKey === key;
			return E('th', { 'class': key === 'address' ? '' : 'ts-numeric',
				'aria-sort': active ? (sortDescending ? 'descending' : 'ascending') : 'none' }, [
				E('button', { type: 'button', 'class': 'ts-sort', 'data-sort': key,
					click: function() {
						sortDescending = sortKey === key ? !sortDescending : key !== 'address';
						sortKey = key;
						update(latest);
						devices.querySelector('[data-sort="' + key + '"]').focus({ preventScroll: true });
					} }, [E('span', { 'class': 'ts-sort-label' }, label), E('span', { 'class': 'ts-sort-indicator', 'aria-hidden': 'true' })])
			]);
		}

		function update(s) {
			latest = s;
			var available = s.available !== false && s.table_exists === true && s.table_owned === true;
			var live = available && s.running === true && !s.stale && s.sampling_ok === true;
			var config = s.configuration || {}, storage = s.storage || {}, interfaces = s.interfaces || {};
			var lans = Array.from(new Set(config.lan_devices || []));
			if (Array.isArray(config.lan_devices) && selectedLan && (lans.length < 2 || lans.indexOf(selectedLan) === -1)) {
				selectedLan = '';
				try { localStorage.removeItem('traffic-stats.lan-filter'); }
				catch (error) { /* Filtering works without persisted preferences. */ }
			}
			lanFilters.hidden = lans.length < 2;
			var nextLanKey = JSON.stringify(lans);
			if (nextLanKey !== lanKey) {
				lanKey = nextLanKey;
				stats.replace(lanFilters.firstChild, [''].concat(lans).map(function(lan) {
					return E('button', { type: 'button', 'class': 'cbi-button ts-refresh-option',
						'data-lan': lan, click: function() {
							selectedLan = lan;
							try { localStorage.setItem('traffic-stats.lan-filter', lan); }
							catch (error) { /* Filtering works without persisted preferences. */ }
							update(latest);
						} }, lan || _('All'));
				}));
			}
			lanFilters.querySelectorAll('button').forEach(function(button) {
				button.setAttribute('aria-pressed', String(button.getAttribute('data-lan') === selectedLan));
			});
			requestAnimationFrame(function() { setScrollFade(lanFilters); });
			var messages = [];
			if (s.load_error)
				messages.push(stats.notice(stats.errorMessage(s.load_error), 'error'));
			else if (!config.enabled)
				messages.push(stats.notice(_('Collection is disabled. Select LAN / WAN devices and enable the service in Settings.')));
			else if (!s.running)
				messages.push(stats.notice(_('Collection has stopped. Saved history is still available. Check the collection configuration in Settings.'), 'warning'));
			else if (s.stale)
				messages.push(stats.notice(_('Collection status is stale. Current rates are unavailable; totals show the last successful sample.'), 'warning'));
			if (s.runtime_error)
				messages.push(stats.notice(_('Cannot read collection status. Check the service and system log.'), 'error'));
			if (s.sampling_ok === false && s.last_error)
				messages.push(stats.notice(_('The latest sample failed. Check the network device settings and system log.'), 'error'));
			if (storage.error)
				messages.push(stats.notice(_('History could not be saved. Pending usage will be retried. Check the database storage and system log.'), 'error'));
			if (s.history_error)
				messages.push(stats.notice(_('The saved device list is unavailable. Current collection data is still shown.'), 'warning'));
			if ((s.invalid_name_rules || []).length)
				messages.push(stats.notice(_('Some device name rules are invalid and were ignored. Check their names and MAC / IP settings.'), 'warning'));
			if (s.known_devices_truncated)
				messages.push(stats.notice(_('Only part of the saved device list is shown because it exceeds the limit.'), 'warning'));
			['lan', 'wan'].forEach(function(side) {
				(interfaces[side] || []).forEach(function(device) {
					if (!device.present || device.carrier === false)
						messages.push(stats.notice((!device.present ? _('%s device %s is not present.') : _('%s device %s is disconnected.')).format(side.toUpperCase(), device.name), 'warning'));
				});
			});
			if (s.flowtables_present)
				messages.push(stats.notice(_('Flow offloading is enabled. Some traffic may bypass accounting, so usage may be incomplete.'), 'warning'));
			if (s.clock_valid === false)
				messages.push(stats.notice(_('System time is not yet valid for history. Usage will be recovered when time is valid and marked as having uncertain timing.'), 'warning'));
			if (s.quality_flags)
				messages.push(stats.notice(_('Sample flags: %s').format(stats.quality(s.quality_flags)), 'warning'));
			stats.replace(notices, messages);
			var statusText = s.load_error || s.runtime_error ? _('Collection status unknown') : !s.running ? _('Collection stopped') :
				s.stale ? _('Samples stale') : s.sampling_ok === false ? _('Sampling failed') : live ? _('Collection normal') : _('Waiting for samples');
			var statusKind = s.load_error || s.runtime_error || !s.running || s.stale ? 'warn' :
				s.sampling_ok === false ? 'bad' : live ? 'good' : 'warn';
			var rates = s.rates || {}, monthly = s.monthly;
			var monthAvailable = monthly && monthly.month === (s.local_date || '').slice(0, 7);
			function monthBytes(counter) {
				return monthAvailable ? stats.bytes(monthly.counters[counter] || '0') : placeholder();
			}
			function monthTotal(prefix) {
				return monthAvailable ? BigInt(monthly.counters[prefix + '_up'] || '0') + BigInt(monthly.counters[prefix + '_down'] || '0') : null;
			}
			function totalBytes(prefix) {
				var value = monthTotal(prefix);
				return value === null ? placeholder() : stats.bytes(value);
			}
			var sampleInterval = Number(s.interval || config.interval);
			statusIndicator.className = 'ts-inline-state ts-state-' + statusKind + (live && statusKind === 'good' ? ' ts-sampling' : '');
			statusTooltip.className = 'ts-status-tooltip ts-state-' + statusKind;
			if (statusTooltip.textContent !== statusText) statusTooltip.textContent = statusText;
			statusIndicator.setAttribute('aria-label', statusText);
			statusIndicator.style.setProperty('--ts-sample-interval', (Number.isFinite(sampleInterval) && sampleInterval > 0 ? sampleInterval : 5) + 's');
			[rateValue(live ? (rates.total_up || {}).bytes_per_second : null),
				rateValue(live ? (rates.total_down || {}).bytes_per_second : null), monthBytes('total_up'), monthBytes('total_down')
			].forEach(function(value, index) { setValue(metricValues[index], value); });
			var list = stats.deviceList(s).filter(function(device) {
				return !selectedLan || !device.local && (device.interfaces || []).indexOf(selectedLan) !== -1;
			});
			function sortValue(device) {
				if (sortKey === 'address')
					return (device.addresses || []).filter(function(address) { return address.indexOf(':') === -1; })[0] || null;
				if (sortKey === 'total') return monthTotal('d_' + device.id);
				var direction = sortKey.indexOf('up') === 0 ? 'up' : 'down';
				if (sortKey.endsWith('_rate')) {
					var value = (rates['d_' + device.id + '_' + direction] || {}).bytes_per_second;
					return live && value != null ? Number(value) : null;
				}
				return monthAvailable ? BigInt(monthly.counters['d_' + device.id + '_' + direction] || '0') : null;
			}
			list.sort(function(a, b) {
				var left = sortValue(a), right = sortValue(b), result = 0;
				if (left === null && right !== null) return 1;
				if (right === null && left !== null) return -1;
				if (left !== null && right !== null)
					result = sortKey === 'address' ? left.localeCompare(right, undefined, { numeric: true }) : left < right ? -1 : left > right ? 1 : 0;
				return (sortDescending ? -result : result) || a.id.localeCompare(b.id);
			});
			function setValue(element, value) {
				if (typeof value === 'string') {
					if (element.childNodes.length !== 1 || element.firstChild.nodeType !== 3 || element.textContent !== value)
						element.textContent = value;
				} else if (element.childNodes.length !== 1 || !element.firstChild.isEqualNode(value)) {
					stats.replace(element, value);
				}
			}
			if (!available) {
				hideUnknown();
				deviceRows.clear();
				deviceTable = null;
				emptyMessage = null;
				stats.replace(devices, stats.empty(_('Waiting for the first sample'),
					_('Enable and start collection to show current device usage. Saved usage is available on the History page.')));
			} else {
				if (!deviceTable) {
					deviceTable = E('table', { 'class': 'ts-table' }, [
						E('thead', {}, [ E('tr', {}, [
							E('th', {}, _('Device')), E('th', {}, _('MAC address')), sortHeader(_('IPv4 address'), 'address'), E('th', {}, _('IPv6 address')),
							sortHeader(_('Upload /s'), 'up_rate'), sortHeader(_('Download /s'), 'down_rate'),
							sortHeader(_('Monthly upload'), 'up'), sortHeader(_('Monthly download'), 'down'), sortHeader(_('Monthly total'), 'total')
						]) ]), E('tbody')
					]);
					stats.replace(devices, deviceTable);
				}
				deviceTable.querySelectorAll('[data-sort]').forEach(function(button) {
					button.parentNode.setAttribute('aria-sort', button.getAttribute('data-sort') === sortKey ? (sortDescending ? 'descending' : 'ascending') : 'none');
				});
				var visible = new Set(list.map(function(device) { return device.id; }));
				if (!selectedLan) visible.add('unknown');
				deviceRows.forEach(function(record, id) {
					if (!visible.has(id)) { if (id === 'unknown') hideUnknown(); record.row.remove(); deviceRows.delete(id); }
				});
				var rows = list.map(function(device) {
					var record = deviceRows.get(device.id);
					if (!record) {
						record = { device: device };
						record.name = E('span', { 'class': 'ts-device-name-text', scroll: updateScrollFade });
						record.badge = E('span', { 'class': 'ts-local-badge' }, _('Local'));
						record.button = E('button', { type: 'button', 'class': 'ts-device-name',
							click: function() { return stats.editDeviceName(record.device, function() { return Promise.resolve(inFlight).then(refresh); }); }
						}, [record.name, E('span', { 'class': 'ts-device-edit', 'aria-hidden': 'true' })]);
						record.addresses = [4, 6].map(function() {
							return E('div', { 'class': 'ts-cell-scroll', tabindex: '0', scroll: updateScrollFade });
						});
						record.row = E('tr', { 'data-device': device.id }, [
							E('td', {}, [E('div', { 'class': 'ts-device-label' }, [record.badge, record.button])]),
							E('td', { 'class': 'ts-mac' }),
							E('td', { 'class': 'ts-addresses' }, record.addresses[0]),
							E('td', { 'class': 'ts-addresses' }, record.addresses[1])
						].concat([0, 1, 2, 3, 4].map(function() { return E('td', { 'class': 'ts-numeric' }); })));
						deviceRows.set(device.id, record);
					}
					record.device = device;
					if (device.local && !record.badge.parentNode) record.button.before(record.badge);
					else if (!device.local) record.badge.remove();
					record.name.classList.toggle('ts-monospace', !device.custom_name && !device.hostname);
					record.button.setAttribute('aria-label', _('Edit device name: %s').format(stats.displayName(device)));
					setValue(record.name, stats.displayName(device));
					setValue(record.row.cells[1], device.mac || device.id);
					record.addresses.forEach(function(element, index) {
						setValue(element, (device.addresses || []).filter(function(address) {
							return (address.indexOf(':') !== -1) === (index === 1);
						}).join(' / ') || placeholder());
					});
					var upload = rates['d_' + device.id + '_up'] || {}, download = rates['d_' + device.id + '_down'] || {};
					[rateValue(live && device.up ? upload.bytes_per_second : null), rateValue(live && device.down ? download.bytes_per_second : null),
						monthBytes('d_' + device.id + '_up'), monthBytes('d_' + device.id + '_down'), totalBytes('d_' + device.id)
					].forEach(function(value, index) { setValue(record.row.cells[index + 4], value); });
					return record.row;
				});
				if (!selectedLan) {
					var unknown = deviceRows.get('unknown');
					if (!unknown) {
						unknown = { row: E('tr', { 'class': 'ts-unknown',
							pointerdown: function(event) { unknownTouch = event.pointerType === 'touch' || event.pointerType === 'pen'; },
							pointerenter: function(event) { if (event.pointerType === 'mouse') showUnknown(event.currentTarget, event); },
							pointermove: function(event) { if (event.pointerType === 'mouse') showUnknown(event.currentTarget, event); },
							pointerleave: function(event) { if (event.pointerType === 'mouse') hideUnknown(); },
							focusin: function(event) { if (!unknownTouch && event.target.matches(':focus-visible')) showUnknown(event.currentTarget); },
							focusout: hideUnknown,
							click: function(event) {
								if (unknownTouch || event.detail === 0 || window.matchMedia('(pointer: coarse)').matches) toggleUnknown(event.currentTarget);
							}
						}, [
							E('td', {}, [E('button', { type: 'button', 'class': 'ts-unknown-label',
								'aria-describedby': 'ts-unknown-tooltip', 'aria-expanded': 'false' }, _('Unattributed traffic'))]),
							E('td', {}, placeholder()), E('td', {}, placeholder()), E('td', {}, placeholder())
						].concat([0, 1, 2, 3, 4].map(function() { return E('td', { 'class': 'ts-numeric' }); }))) };
						deviceRows.set('unknown', unknown);
					}
					[rateValue(live ? (rates.unknown_up || {}).bytes_per_second : null), rateValue(live ? (rates.unknown_down || {}).bytes_per_second : null),
						monthBytes('unknown_up'), monthBytes('unknown_down'), totalBytes('unknown')
					].forEach(function(value, index) { setValue(unknown.row.cells[index + 4], value); });
					rows.push(unknown.row);
				}
				var body = deviceTable.tBodies[0], next = body.firstChild;
				rows.forEach(function(row) {
					if (row !== next) {
						if (body.moveBefore && row.parentNode === body) body.moveBefore(row, next);
						else body.insertBefore(row, next);
					}
					next = row.nextSibling;
				});
				if (list.length && emptyMessage) { emptyMessage.remove(); emptyMessage = null; }
				if (!list.length) {
					var message = stats.empty(selectedLan ? _('No devices on this network') : _('No downstream devices found'), selectedLan ? '' : _('Check the LAN connection and device addresses. No usage can currently be attributed to a device.'));
					if (!emptyMessage || !emptyMessage.isEqualNode(message)) {
						if (emptyMessage) emptyMessage.remove();
						emptyMessage = message;
						devices.appendChild(emptyMessage);
					}
				}
			}
			scrollObserver.disconnect();
			scrollObserver.observe(lanFilters);
			scrollObserver.observe(lanFilters.firstChild);
			devices.querySelectorAll('.ts-device-name-text, .ts-cell-scroll').forEach(function(element) {
				scrollObserver.observe(element);
				setScrollFade(element);
			});

		}

		function setRefreshAnimation() {
			page.style.setProperty('--ts-refresh-interval', refreshInterval + 's');
		}

		function refresh() {
			if (inFlight)
				return inFlight;
			inFlight = stats.status().then(update).catch(function(error) {
				update(Object.assign({}, latest, { load_error: error, stale: true, sampling_ok: false }));
			}).finally(function() {
				inFlight = null;
			});
			return inFlight;
		}

		update(initial);
		poll.add(refresh, refreshInterval);
		setRefreshAnimation();
		page.appendChild(stats.footer());
		return page;
	},
	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
