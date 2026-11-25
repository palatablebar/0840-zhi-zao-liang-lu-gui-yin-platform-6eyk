(function () {
  var state = { shiftId: null, shift: null, plan: null, approved: false };
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
    });
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function setNotice(text) { $('notice').textContent = text || ''; }
  function minuteText(moment, base) {
    if (!moment) return '';
    return moment.slice(11, 16);
  }
  var ROLE = { STARTED: '已开工', URGENT: '急单', SALVAGE: '救料' };
  var STATUS = { READY: '可排', INCOMPLETE: '待补录', REVOKED: '已撤销', UNASSIGNED: '未定工位' };

  function renderJobs() {
    var jobs = (state.shift && state.shift.jobs) || [];
    $('jobs-hint').textContent = jobs.length + ' 张返工单';
    $('job-body').innerHTML = jobs.length ? jobs.map(function (job) {
      return '<tr class="job-' + esc(job.status) + '">' +
        '<td>' + esc(job.job_id) + '</td>' +
        '<td>' + esc(job.work_order || '-') + '</td>' +
        '<td>' + esc(job.station_id || '-') + '</td>' +
        '<td class="num">' + esc(job.expected_minutes === null ? '待补' : job.expected_minutes) + '</td>' +
        '<td>' + esc(job.due_at ? job.due_at.replace('T', ' ').slice(0, 16) : '待补') + '</td>' +
        '<td class="num">' + esc(job.salvage_qty === null ? '待补' : job.salvage_qty) + '</td>' +
        '<td><span class="badge">' + esc(STATUS[job.status] || job.status) + '</span></td>' +
        '</tr>';
    }).join('') : '<tr><td class="empty" colspan="7">还没有返工单，先在右侧补录</td></tr>';
  }

  function renderGantt() {
    var plan = state.plan;
    if (!plan || !plan.schedule) { $('gantt').innerHTML = '<p class="empty">生成排程后显示工位安排</p>'; return; }
    var starts = new Date(plan.starts_at).getTime();
    var ends = new Date(plan.ends_at).getTime();
    var span = Math.max(1, ends - starts);
    $('gantt').innerHTML = plan.schedule.stations.map(function (station) {
      var blocks = station.items.map(function (item) {
        var left = (new Date(item.start_at).getTime() - starts) / span * 100;
        var width = (new Date(item.end_at).getTime() - new Date(item.start_at).getTime()) / span * 100;
        var title = item.job_id + ' · ' + ROLE[item.role] + ' · ' + minuteText(item.start_at) + '–' + minuteText(item.end_at) + ' · 救 ' + item.salvage_qty + ' 件' + (item.on_time ? '' : ' · 迟到');
        return '<i class="block ' + esc(item.role.toLowerCase()) + (item.on_time ? '' : ' late') + '" style="left:' + left + '%;width:' + width + '%" title="' + esc(title) + '"><span>' + esc(item.job_id) + '</span></i>';
      }).join('');
      var idle = station.idle_minutes > 0 ? '<i class="block idle" style="left:' + (station.used_minutes / station.capacity_minutes * 100) + '%;width:' + (station.idle_minutes / station.capacity_minutes * 100) + '%"></i>' : '';
      return '<div class="track"><span class="track-label">' + esc(station.station_id) + '<small>' + station.used_minutes + '/' + station.capacity_minutes + ' 分</small></span><div class="rail-track">' + blocks + idle + '</div></div>';
    }).join('');
    var totals = plan.schedule.totals;
    $('plan-hint').textContent = 'rev ' + plan.schedule.revision + ' · 排 ' + totals.jobs_scheduled + ' 张 · 救 ' + totals.salvaged + ' 件';
  }

  function renderTotals() {
    var plan = state.plan;
    var totals = plan && plan.schedule ? plan.schedule.totals : null;
    $('k-capacity').textContent = totals ? totals.capacity_minutes : '--';
    $('k-used').textContent = totals ? totals.used_minutes : '--';
    $('k-salvaged').textContent = totals ? totals.salvaged : '--';
    $('k-urgent').textContent = totals ? (totals.urgent_on_time + '/' + totals.urgent_total) : '--';
  }

  function renderDeferred() {
    var plan = state.plan;
    var rows = plan && plan.schedule ? plan.schedule.deferred.slice() : [];
    if (plan && plan.schedule) {
      plan.schedule.issues.forEach(function (issue) {
        rows.push({ job_id: issue.job_id, reason: issue.code, detail: issue.detail, station_id: '-' });
      });
    }
    $('deferred-count').textContent = rows.length + ' 条';
    $('deferred-list').innerHTML = rows.length ? rows.map(function (item) {
      return '<li><span>' + esc(item.job_id) + '</span><span>' + esc(item.station_id || '-') + '</span><code>' + esc(item.reason) + '</code><small>' + esc(item.detail || '') + '</small></li>';
    }).join('') : '<li class="empty">全部已排入</li>';
  }

  function render() {
    renderJobs();
    renderGantt();
    renderTotals();
    renderDeferred();
    var revision = state.shift ? state.shift.revision : 0;
    $('revision').textContent = 'rev ' + revision;
    var approved = state.shift && state.shift.review && state.shift.review.decision === 'APPROVED';
    $('approve').disabled = !state.plan || !state.plan.schedule || approved;
    $('hold').disabled = !state.plan || !state.plan.schedule;
    $('download').hidden = !approved;
    if (approved) $('download').href = '/api/shifts/' + state.shiftId + '/evidence';
    $('shift-status').textContent = state.shiftId + (approved ? ' · 已签发' : (state.plan && state.plan.schedule ? ' · DRAFT' : ' · 未排程'));
  }

  async function loadReports() {
    var data = await request('/api/reports');
    var select = $('report-input');
    select.innerHTML = data.reports.length ? data.reports.map(function (report) {
      var label = report.run_id + ' · ' + report.profile + ' · ' + (report.work_orders.join('/') || '无工单');
      return '<option value="' + esc(report.run_id) + '">' + esc(label) + '</option>';
    }).join('') : '<option value="">没有已签发报工</option>';
    $('report-hint').textContent = data.reports.length ? (data.reports.length + ' 个可排报工') : '请先在批次归因签发报工';
  }

  function parseStations(text) {
    return text.split(/[,，;；\n]/).map(function (part) {
      part = part.trim();
      if (!part) return null;
      var cells = part.split(/[:：]/);
      return { station_id: cells[0].trim(), name: (cells[1] || cells[0]).trim(), minutes: Number(cells[2]) };
    }).filter(Boolean);
  }

  async function submitShift(event) {
    event.preventDefault();
    var form = new FormData($('shift-form'));
    var payload = {
      shift_id: form.get('shift_id'),
      starts_at: form.get('starts_at') + ':00',
      ends_at: form.get('ends_at') + ':00',
      stations: parseStations(form.get('stations') || '')
    };
    try {
      setNotice('');
      await request('/api/shifts', {method: 'POST', body: JSON.stringify(payload)});
      await loadShifts(payload.shift_id);
    } catch (error) { setNotice(error.message); }
  }

  async function loadShift() {
    if (!state.shiftId) return;
    var data = await request('/api/shifts/' + state.shiftId);
    state.shift = data;
    state.plan = data;
    var select = $('station-input');
    select.innerHTML = data.stations.map(function (station) {
      return '<option value="' + esc(station.station_id) + '">' + esc(station.name) + '</option>';
    }).join('');
    render();
  }

  async function loadShifts(preferId) {
    var data = await request('/api/shifts');
    var select = $('shift-select');
    if (!data.shifts.length) {
      select.innerHTML = '<option value="">暂无班次</option>';
      state.shiftId = null;
      render();
      return;
    }
    select.innerHTML = data.shifts.map(function (shift) {
      return '<option value="' + esc(shift.shift_id) + '">' + esc(shift.shift_id) + ' · ' + shift.starts_at.slice(11, 16) + '–' + shift.ends_at.slice(11, 16) + (shift.reviewed ? ' · 已签发' : '') + '</option>';
    }).join('');
    if (preferId && data.shifts.some(function (shift) { return shift.shift_id === preferId; })) {
      select.value = preferId;
    }
    state.shiftId = select.value;
    await loadShift();
  }

  async function submitJob(event) {
    event.preventDefault();
    if (!state.shiftId) return;
    var form = new FormData($('job-form'));
    var payload = {};
    form.forEach(function (value, key) { payload[key] = value === '' ? null : value; });
    ['expected_minutes', 'salvage_qty', 'report_qty', 'received_qty'].forEach(function (key) {
      if (payload[key] !== null) payload[key] = Number(payload[key]);
    });
    if (payload.due_at) payload.due_at = payload.due_at.length === 16 ? payload.due_at + ':00' : payload.due_at;
    try {
      setNotice('');
      await request('/api/shifts/' + state.shiftId + '/jobs', {method: 'POST', body: JSON.stringify(payload)});
      await loadShift();
    } catch (error) { setNotice(error.message); }
  }

  async function schedule() {
    if (!state.shiftId) return;
    try {
      setNotice('');
      await request('/api/shifts/' + state.shiftId + '/schedule', {method: 'POST', body: JSON.stringify({})});
      await loadShift();
    } catch (error) { setNotice(error.message); }
  }

  async function review(decision) {
    if (!state.shiftId || !state.plan) return;
    try {
      setNotice('');
      await request('/api/shifts/' + state.shiftId + '/review', {
        method: 'POST',
        body: JSON.stringify({decision: decision, comment: $('comment').value, expected_revision: state.plan.revision})
      });
      await loadShift();
    } catch (error) { setNotice(error.message); }
  }

  async function boot() {
    try {
      await loadShifts();
      await loadReports();
    } catch (error) { setNotice(error.message); }
    var shiftForm = $('shift-form');
    if (shiftForm) shiftForm.addEventListener('submit', submitShift);
    $('shift-select').addEventListener('change', function () { state.shiftId = this.value; loadShift(); });
    $('reload').addEventListener('click', function () { loadShifts().catch(function (error) { setNotice(error.message); }); });
    $('job-form').addEventListener('submit', submitJob);
    $('schedule').addEventListener('click', schedule);
    $('approve').addEventListener('click', function () { review('APPROVED'); });
    $('hold').addEventListener('click', function () { review('HELD'); });
  }
  document.addEventListener('DOMContentLoaded', boot);
})();
