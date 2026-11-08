'use strict';
'require view';
'require traffic-stats as stats';

function svg(tag, attrs, text) {
	var node = document.createElementNS('http://www.w3.org/2000/svg', tag);
	Object.keys(attrs || {}).forEach(function(key) { node.setAttribute(key, attrs[key]); });
	if (text != null) node.textContent = text;
	return node;
}
function smoothPath(points) {
	var slopes = points.slice(1).map(function(point, index) {
		return (point[1] - points[index][1]) / (point[0] - points[index][0]);
	});
	var tangents = points.map(function(point, index) {
		if (!index) return slopes[0] || 0;
		if (index === points.length - 1) return slopes[index - 1] || 0;
		var before = slopes[index - 1], after = slopes[index];
		return before * after <= 0 ? 0 : Math.sign(before) * Math.min(Math.abs(before + after) / 2, 2 * Math.abs(before), 2 * Math.abs(after));
	});
	return points.map(function(point, index) {
		if (!index) return 'M' + point.join(' ');
		var previous = points[index - 1], step = (point[0] - previous[0]) / 3;
		return 'C' + [previous[0] + step, previous[1] + step * tangents[index - 1],
			point[0] - step, point[1] - step * tangents[index], point[0], point[1]].join(' ');
	}).join(' ');
}

function button(label, click, cls) {
	return E('button', { type: 'button', 'class': 'cbi-button ' + (cls || ''), click: click }, [label]);
}
function sumRows(results) {
	var days = {};
	results.forEach(function(result, index) {
		(result.rows || []).forEach(function(row) {
			var item = days[row.day] || (days[row.day] = { up: 0n, down: 0n });
			item[index ? 'down' : 'up'] += stats.integer(row.bytes);
		});
	});
	return days;
}

return view.extend({
	load: function() { return stats.status(); },
	render: function(status) {
		stats.loadStyle();
		var language = document.documentElement.lang || 'en';
		var monthFormat = new Intl.DateTimeFormat(language, { year: 'numeric', month: 'long', timeZone: 'UTC' });
		var weekdayFormat = new Intl.DateTimeFormat(language, { weekday: /^zh(?:-|$)/i.test(language) ? 'narrow' : 'short', timeZone: 'UTC' });
		var today = status.local_date || new Date().toISOString().slice(0, 10);
		var selected = today, month = today.slice(0, 7), monthSequence = 0, daySequence = 0;
		var daily = {}, detail = [], deviceData = null, deviceDay = '', lastMonthLabel = '', chartSize = '';
		var devices = stats.deviceList(status), byId = {};
		devices.forEach(function(item) { byId['d_' + item.id] = item; });
		var selection = new URLSearchParams(window.location.search).get('device');
		var target = /^[0-9a-f]{12}$/.test(selection || '') ? 'd_' + selection : 'total';
		if (target !== 'total' && !byId[target]) {
			var missing = { id: selection, mac: selection.match(/../g).join(':') };
			devices.push(missing); byId[target] = missing;
		}
		var deviceSelect = E('select', { 'class': 'ts-history-device-select', 'aria-label': _('Select device'),
			change: function() { selectDevice(deviceSelect.value); } },
			['total'].concat(devices.filter(function(device) { return device.local; }).map(function(device) { return 'd_' + device.id; }),
				['unknown'], devices.filter(function(device) { return !device.local; }).map(function(device) { return 'd_' + device.id; })).map(function(key) {
				return E('option', { value: key, 'class': usesAddressName(key) ? 'ts-monospace' : '' }, [deviceOptionName(key)]);
			}));
		deviceSelect.value = target;
		deviceSelect.classList.toggle('ts-monospace', usesAddressName(target));
		var monthInput = E('input', { type: 'month', value: month, max: today.slice(0, 7), 'aria-label': _('Select month'), lang: language,
			'class': 'ts-month-picker', click: function(event) {
				if (typeof monthInput.showPicker !== 'function') return;
				try { monthInput.showPicker(); event.preventDefault(); } catch (error) { /* Keep native input behavior when a browser disallows the picker. */ }
			},
				change: function() { setMonth(monthInput.value); } });
		var metricValues = [0, 1, 2].map(function() { return E('strong', {}, ['—']); });
		var metricCards = [_('Monthly upload'), _('Monthly download'), _('Monthly total')].map(function(label, index) {
			return E('div', { 'class': 'ts-metric' + (index < 2 ? ' ts-metric-' + (index === 0 ? 'up' : 'down') : '') }, [
				E('span', { 'class': 'ts-metric-label', 'data-loading-label': ' · ' + _('Updating') }, [
					index < 2 ? E('span', { 'class': 'ts-metric-icon', 'aria-hidden': 'true' }, index === 0 ? '↑' : '↓') : null, label
				].filter(Boolean)), metricValues[index]
			]);
		});
		var metrics = E('div', { 'class': 'ts-metrics ts-history-metrics' }, [
			metricCards[2], E('div', { 'class': 'ts-history-directions' }, [
				metricCards[0], metricCards[1]
			])
		]);
		var calendar = E('div', { 'class': 'ts-calendar-grid' });
		var calendarTitle = E('strong');
		var message = E('div', { 'class': 'ts-history-message', 'aria-live': 'polite' });
		var dayMessage = E('div', { 'aria-live': 'polite' });
		var chart = E('div', { 'class': 'ts-history-chart' });
		var chartTitle = E('h3');
		var table = E('div', { 'class': 'ts-table-scroll' });
		var page = E('div', { 'class': 'ts-page ts-history-page' }, [
			message, metrics,
			E('div', { 'class': 'ts-history-layout' }, [
				E('section', { 'class': 'ts-section ts-calendar' }, [
					E('div', { 'class': 'ts-toolbar' }, [calendarTitle, E('div', { 'class': 'ts-history-controls' }, [
						button('‹', function() { shiftMonth(-1); }), monthInput, button('›', function() { shiftMonth(1); })])]),
					calendar]),
				E('section', { 'class': 'ts-section ts-day-curve' }, [
					E('div', { 'class': 'ts-toolbar' }, [
						E('div', { 'class': 'ts-history-controls ts-chart-heading' }, [chartTitle,
							E('div', { 'class': 'ts-history-controls ts-history-legend' }, [E('span', { style: 'color:#18a999' }, ['● ' + _('Upload')]), E('span', { style: 'color:var(--ts-down)' }, ['● ' + _('Download')])])]),
						deviceSelect]),
					dayMessage, chart])]),
			E('section', { 'class': 'ts-section ts-history-devices' }, [table])
		]);
		function deviceName(key) {
			return key === 'total' ? _('All devices') : key === 'unknown' ? _('Unidentified devices') :
				byId[key] ? stats.displayName(byId[key]) : key.slice(2).match(/../g).join(':');
		}
		function isLocal(key) { return !!(byId[key] && byId[key].local); }
		function usesAddressName(key) {
			return key !== 'total' && key !== 'unknown' && (!byId[key] || !byId[key].custom_name && !byId[key].hostname);
		}
		function deviceOptionName(key) { return isLocal(key) ? _('%s (Local)').format(deviceName(key)) : deviceName(key); }
		function addDeviceOption(key) {
			if (!Array.from(deviceSelect.options).some(function(option) { return option.value === key; }))
				deviceSelect.appendChild(E('option', { value: key, 'class': usesAddressName(key) ? 'ts-monospace' : '' }, [deviceOptionName(key)]));
		}
		function selectDevice(key) {
			target = key;
			addDeviceOption(key); deviceSelect.value = key;
			deviceSelect.classList.toggle('ts-monospace', usesAddressName(key));
			table.querySelectorAll('tr[data-device]').forEach(function(row) {
				row.classList.toggle('is-selected', row.getAttribute('data-device') === key);
			});
			refresh(false);
			for (var container = page.parentElement; container; container = container.parentElement) {
				if (container.scrollTop) container.scrollTo({ top: 0, behavior: 'smooth' });
			}
		}
		function setMonth(value) {
				if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(value) || value > today.slice(0, 7) || value < '1970-01') { monthInput.value = month; return; }
			month = value; monthInput.value = month;
			selected = month === today.slice(0, 7) ? today : month + '-01';
			refresh();
		}
		function shiftMonth(offset) {
			var date = new Date(month + '-01T00:00:00Z'); date.setUTCMonth(date.getUTCMonth() + offset);
			setMonth(date.toISOString().slice(0, 7));
		}
		function renderCalendar() {
			calendarTitle.textContent = monthFormat.format(new Date(month + '-01T00:00:00Z'));
			var first = new Date(month + '-01T00:00:00Z'), count = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
			var cells = [0, 1, 2, 3, 4, 5, 6].map(function(day) {
				return E('span', { 'class': 'ts-weekday' }, [weekdayFormat.format(new Date(Date.UTC(2024, 0, day + 1)))]);
			});
			for (var blank = 0; blank < (first.getUTCDay() + 6) % 7; blank++) cells.push(E('span'));
			var largest = Object.keys(daily).reduce(function(max, key) { return Math.max(max, Number(daily[key].up + daily[key].down)); }, 0);
			for (var n = 1; n <= count; n++) {
				(function(day) {
					var key = month + '-' + String(day).padStart(2, '0'), record = daily[key], amount = record ? record.up + record.down : 0n;
					var level = amount > 0n ? Math.max(1, Math.ceil(Number(amount) / largest * 4)) : 0;
					var cell = button('', function() { selected = key; renderCalendar(); loadDay(); }, 'ts-calendar-day ts-heat-' + level + (selected === key ? ' is-selected' : ''));
					cell.disabled = key > today;
					cell.setAttribute('aria-pressed', String(selected === key));
					cell.setAttribute('aria-label', _('%s: %s').format(key, record ? stats.bytes(amount) : _('No records')));
					stats.replace(cell, [E('strong', {}, [String(day)]), E('small', {}, [record ? stats.bytes(amount) : '—'])]);
					cells.push(cell);
				})(n);
			}
			stats.replace(calendar, cells);
		}
		function refresh(reloadDevices) {
			var sequence = ++monthSequence, key = target, start = month + '-01';
			var end = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5)), 0)).toISOString().slice(0, 10);
			daily = {}; renderCalendar(); stats.replace(message, []);
			metrics.setAttribute('aria-busy', 'true');
			loadDay(reloadDevices);
			return Promise.all(['up', 'down'].map(function(direction) { return stats.history('day', start, end, key + '_' + direction); })).then(function(results) {
				if (sequence !== monthSequence) return;
				daily = sumRows(results); renderCalendar();
				var up = 0n, down = 0n; Object.keys(daily).forEach(function(day) { up += daily[day].up; down += daily[day].down; });
				[up, down, up + down].forEach(function(value, index) { metricValues[index].textContent = stats.bytes(value); });
				lastMonthLabel = _('%s · %s').format(month, deviceName(key));
				if (results.some(function(result) { return result.truncated; })) stats.replace(message, stats.notice(_('The query limit was exceeded. Monthly totals include only the returned records.'), 'warning'));
			}).catch(function(error) {
				if (sequence === monthSequence) stats.replace(message, stats.notice((lastMonthLabel ? _('Monthly usage could not be updated. Still showing %s. %s').format(lastMonthLabel, stats.errorMessage(error)) : _('Monthly usage could not be updated. %s').format(stats.errorMessage(error))), 'warning'));
			}).finally(function() { if (sequence === monthSequence) metrics.setAttribute('aria-busy', 'false'); });
		}
		function loadDay(reloadDevices) {
			var sequence = ++daySequence, day = selected, key = target;
			var fetchDevices = reloadDevices !== false || deviceDay !== day || !deviceData;
			detail = []; chartTitle.textContent = day;
			var oldTooltip = chart.querySelector('.ts-curve-tooltip'), oldHover = chart.querySelector('.ts-curve-hover');
			if (oldTooltip) oldTooltip.hidden = true;
			if (oldHover) oldHover.style.display = 'none';
			if (!chart.firstChild) stats.replace(chart, [E('p', { 'class': 'ts-muted' }, [_('Loading...')])]);
			chart.setAttribute('aria-busy', 'true'); chart.inert = true;
			table.setAttribute('aria-busy', String(fetchDevices)); stats.replace(dayMessage, []);
			return Promise.all([stats.history('30min', day, day, key + '_up'), stats.history('30min', day, day, key + '_down'),
				fetchDevices ? stats.history('day', day, day, '') : Promise.resolve(deviceData)]).then(function(results) {
				if (sequence !== daySequence) return;
				detail = results.slice(0, 2); draw();
				if (fetchDevices) { deviceData = results[2]; deviceDay = day; renderDevices(deviceData); }
				if (results.some(function(result) { return result.truncated || result.timeline_truncated || result.multiple_timezones; }))
					stats.replace(dayMessage, stats.notice(_('Some records are truncated or use different collection timezones. Check the recorded timezones.'), 'warning'));
			}).catch(function(error) {
				if (sequence !== daySequence) return;
				stats.replace(dayMessage, stats.notice(_('Daily usage could not be updated. The chart and list still show the previous results. %s').format(stats.errorMessage(error)), 'warning'));
			}).finally(function() {
				if (sequence !== daySequence) return;
				chart.setAttribute('aria-busy', 'false'); chart.inert = false;
				table.setAttribute('aria-busy', 'false');
			});
		}
		function renderDevices(result) {
			var grouped = {};
			(result.rows || []).forEach(function(row) {
				var match = /^(d_[0-9a-f]{12}|unknown)_(up|down)$/.exec(row.counter);
				if (!match) return;
				var item = grouped[match[1]] || (grouped[match[1]] = { key: match[1], up: 0n, down: 0n });
				item[match[2]] += stats.integer(row.bytes);
			});
			var rows = Object.keys(grouped).map(function(key) { return grouped[key]; }).filter(function(row) {
				return !isLocal(row.key) || row.up > 0n || row.down > 0n;
			}).sort(function(a, b) {
				return a.down > b.down ? -1 : a.down < b.down ? 1 : a.key.localeCompare(b.key);
			});
			rows.forEach(function(row) { addDeviceOption(row.key); });
			if (!rows.length) { stats.replace(table, stats.empty(_('No device usage for this day'), _('Select another date in the calendar.'))); return; }
			stats.replace(table, E('table', { 'class': 'ts-table' }, [
				E('thead', {}, [E('tr', {}, [_('Device'), _('Upload'), _('Download'), _('Total')].map(function(label) { return E('th', {}, [label]); }))]),
				E('tbody', {}, rows.map(function(row) {
					var name = isLocal(row.key) ? [E('span', { 'class': 'ts-local-badge' }, [_('Local')])] : [];
					name.push(button(E('span', { 'class': usesAddressName(row.key) ? 'ts-monospace' : '' }, [deviceName(row.key)]), function() { selectDevice(row.key); }, 'ts-history-device-link'));
					return E('tr', { 'class': row.key === target ? 'is-selected' : '', 'data-device': row.key }, [E('td', {}, name)].concat(
						[row.up, row.down, row.up + row.down].map(function(value) { return E('td', { 'class': 'ts-numeric' }, [stats.bytes(value)]); })));
				}))]));
		}
		function points() {
			var slots = {}, values = [{}, {}];
			detail.forEach(function(result, direction) {
				(result.slots || []).forEach(function(slot) { slots[slot.start + '|' + slot.timezone] = slot; });
				(result.rows || []).forEach(function(row) { values[direction][row.bucket_start + '|' + row.timezone] = row; });
			});
			return Object.keys(slots).map(function(key) {
				var slot = slots[key];
				var point = { start: slot.start, end: slot.end, label: slot.label, endLabel: slot.end_label, zone: slot.timezone, up: 0n, down: 0n, coverage: [0, 0], seen: [false, false], flags: [0, 0] };
				[0, 1].forEach(function(direction) {
					var row = values[direction][key]; if (!row) return;
					point[direction ? 'down' : 'up'] = stats.integer(row.bytes); point.seen[direction] = true;
					point.coverage[direction] = Math.min(Number(row.coverage_ms) || 0, (slot.end - slot.start) * 1000); point.flags[direction] = row.flags || 0;
				});
				return point;
			}).sort(function(a, b) { return a.start - b.start || a.zone.localeCompare(b.zone); });
		}
		function draw() {
			var data = points(), observed = data.some(function(point) { return point.seen.some(Boolean); });
			var width = chart.clientWidth || 760, height = chart.clientHeight || 270;
			var left = width < 480 ? 54 : 66, right = width < 480 ? 8 : 18, top = 12, bottom = 28, max = 1024;
			chartSize = width + ':' + height;
			function value(point, direction) { return Number(point[direction ? 'down' : 'up']); }
			function incomplete(point, direction) { return point.flags[direction] || point.coverage[direction] < (point.end - point.start) * 1000; }
			data.forEach(function(point) { max = Math.max(max, value(point, 0), value(point, 1)); }); max *= 1.12;
			function x(index) { return left + (width - left - right) * (index + 0.5) / data.length; }
			function y(value) { return height - bottom - value / max * (height - top - bottom); }
			var canvas = svg('svg', { viewBox: '0 0 ' + width + ' ' + height, role: 'img', 'aria-label': _('Upload and download usage every 30 minutes on %s').format(selected) });
			for (var tick = 0; tick <= 4; tick++) {
				var amount = max * tick / 4;
				canvas.appendChild(svg('line', { x1: left, x2: width - right, y1: y(amount), y2: y(amount), 'class': 'ts-chart-grid' }));
				canvas.appendChild(svg('text', { x: left - 8, y: y(amount) + 4, 'text-anchor': 'end' }, observed || tick === 0 ? stats.bytes(Math.round(amount)) : ''));
			}
			var hourStep = width < 300 ? 12 : width < 560 ? 6 : 3;
			for (var hour = 0; hour <= 24; hour += hourStep) {
				var label = String(hour).padStart(2, '0') + ':00';
				var index = data.findIndex(function(point) { return point.label === label; });
				var position = hour === 24 ? 1 : index >= 0 ? index / data.length : hour / 24;
				canvas.appendChild(svg('text', { x: left + (width - left - right) * position, y: height - 12, 'text-anchor': hour === 24 ? 'end' : hour === 0 ? 'start' : 'middle', 'class': 'ts-chart-time' }, label));
			}
			var definitions = svg('defs'), areas = svg('g'), curves = svg('g'), markers = svg('g');
			canvas.appendChild(definitions);
			[0, 1].forEach(function(direction) {
				var color = direction ? '#3479ed' : '#18a999', gradientId = 'ts-history-fill-' + direction;
				var gradient = svg('linearGradient', { id: gradientId, x1: '0%', y1: '0%', x2: '0%', y2: '100%' });
				gradient.appendChild(svg('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': '.27' }));
				gradient.appendChild(svg('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': '.015' }));
				definitions.appendChild(gradient);
				var segments = [], segment = [], previous = null;
				data.forEach(function(point, index) {
					if (!point.seen[direction]) { previous = null; return; }
					if (!previous || previous.end !== point.start || previous.zone !== point.zone) {
						segment = [];
						segments.push(segment);
					}
					segment.push([x(index), y(value(point, direction))]);
					var partial = incomplete(point, direction);
					markers.appendChild(svg('circle', { 'class': 'ts-curve-point' + (partial ? ' is-incomplete' : ''), cx: x(index), cy: y(value(point, direction)), r: partial ? 3.5 : 2.5,
						fill: partial ? 'var(--ts-card)' : color, stroke: color, 'stroke-width': partial ? 1.7 : 0 }));
					previous = point;
				});
				segments.forEach(function(points) {
					if (points.length < 2) return;
					var path = smoothPath(points), first = points[0], last = points[points.length - 1];
					areas.appendChild(svg('path', { 'class': 'ts-curve-area', d: path + ' L' + last[0] + ' ' + y(0) + ' L' + first[0] + ' ' + y(0) + ' Z', fill: 'url(#' + gradientId + ')' }));
					curves.appendChild(svg('path', { 'class': 'ts-curve-line', d: path, fill: 'none', stroke: color, 'stroke-width': 2.3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
				});
			});
			canvas.appendChild(areas);
			canvas.appendChild(curves);
			canvas.appendChild(markers);
			var guide = svg('line', { y1: top, y2: height - bottom, 'class': 'ts-curve-guide' });
			var hover = svg('g', { 'class': 'ts-curve-hover', 'aria-hidden': 'true', style: 'display:none;pointer-events:none' });
			hover.appendChild(guide);
			var dots = [0, 1].map(function(direction) {
				var dot = svg('circle', { r: 5, fill: direction ? '#3479ed' : '#18a999', stroke: 'var(--ts-card)', 'stroke-width': 2 });
				hover.appendChild(dot); return dot;
			});
			canvas.appendChild(hover);
			var hoverTime = E('strong'), hoverValues = [E('strong'), E('strong')], hoverQuality = E('small', { 'class': 'ts-muted' });
			var tooltip = E('div', { 'class': 'ts-curve-tooltip', hidden: true, role: 'status' }, [hoverTime].concat(
				[_('Upload'), _('Download')].map(function(label, direction) {
					return E('div', { 'class': 'ts-curve-tooltip-row' }, [E('span', { style: 'color:' + (direction ? '#3479ed' : '#18a999') }, ['● ' + label]), hoverValues[direction]]);
				}), [hoverQuality]));
			function hideHover() { hover.style.display = 'none'; tooltip.hidden = true; }
			function showHover(event) {
				var box = canvas.getBoundingClientRect(), outer = chart.getBoundingClientRect();
				var mouseX = (event.clientX - box.left) / box.width * width;
				if (mouseX < left || mouseX > width - right) { hideHover(); return; }
				var index = Math.min(data.length - 1, Math.floor((mouseX - left) / (width - left - right) * data.length)), point = data[index];
				if (!point || !point.seen.some(Boolean)) { hideHover(); return; }
				hover.style.display = '';
				guide.setAttribute('x1', x(index)); guide.setAttribute('x2', x(index));
				dots.forEach(function(dot, direction) {
					dot.style.display = point.seen[direction] ? '' : 'none';
					dot.setAttribute('cx', x(index)); dot.setAttribute('cy', y(value(point, direction)));
					dot.setAttribute('fill', incomplete(point, direction) ? 'var(--ts-card)' : direction ? '#3479ed' : '#18a999');
					dot.setAttribute('stroke', incomplete(point, direction) ? direction ? '#3479ed' : '#18a999' : 'var(--ts-card)');
					hoverValues[direction].textContent = point.seen[direction] ? stats.bytes(direction ? point.down : point.up) : _('No records');
				});
				hoverTime.textContent = point.label + ' - ' + (point.endLabel === '00:00' ? '24:00' : point.endLabel);
				hoverQuality.textContent = [0, 1].some(function(direction) { return incomplete(point, direction); }) ? _('Incomplete records / uncertain timing') : '';
				tooltip.hidden = false;
				tooltip.style.left = Math.max(0, Math.min(outer.width - tooltip.offsetWidth, box.left - outer.left + x(index) / width * box.width + 12)) + 'px';
				tooltip.style.top = (box.top - outer.top + 12) + 'px';
			}
			canvas.addEventListener('pointermove', showHover);
			canvas.addEventListener('pointerdown', function(event) {
				if (event.pointerType === 'mouse') return;
				canvas.setPointerCapture(event.pointerId);
				showHover(event);
			});
			// A touch selection stays visible after lifting the finger; a vertical scroll cancels it.
			canvas.addEventListener('pointerleave', function(event) { if (event.pointerType === 'mouse') hideHover(); });
			canvas.addEventListener('pointercancel', hideHover);
			stats.replace(chart, [canvas, tooltip].concat(observed ? [] : [E('p', { 'class': 'ts-muted ts-curve-empty' }, [_('No detailed records for this day.')])]));
		}
		var chartObserver = new ResizeObserver(function() {
			if (!page.isConnected) { chartObserver.disconnect(); return; }
			if (detail.length && chartSize !== chart.clientWidth + ':' + chart.clientHeight) draw();
		});
		chartObserver.observe(chart);
		refresh();
		page.appendChild(stats.footer());
		return page;
	},
	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
