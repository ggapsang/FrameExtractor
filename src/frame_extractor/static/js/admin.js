// FrameExtractor — vanilla JS admin glue.

(function () {
  'use strict';

  // --- HTTP helpers ------------------------------------------------------
  async function jsonFetch(url, opts) {
    const res = await fetch(url, opts || {});
    let body = null;
    try { body = await res.json(); } catch (_) { /* may be empty */ }
    if (!res.ok) {
      const msg = (body && body.detail) || res.statusText || ('HTTP ' + res.status);
      throw new Error(msg);
    }
    return body;
  }

  // --- Upload form ------------------------------------------------------
  // Supports two pickers: file (multiple) and folder (webkitdirectory).
  // A radio switches which input is visible/active. The active input's
  // FileList is appended to a single multipart `files` field.
  function bindUpload(formEl, statusEl) {
    if (!formEl) return;
    const filesInput = formEl.querySelector('#upload-input-files');
    const folderInput = formEl.querySelector('#upload-input-folder');
    const modeRadios = formEl.querySelectorAll('input[name="upload-mode"]');

    function activeMode() {
      const checked = formEl.querySelector('input[name="upload-mode"]:checked');
      return checked ? checked.value : 'files';
    }

    function applyModeVisibility() {
      const mode = activeMode();
      if (filesInput) filesInput.style.display = (mode === 'files') ? '' : 'none';
      if (folderInput) folderInput.style.display = (mode === 'folder') ? '' : 'none';
    }

    modeRadios.forEach(function (r) {
      r.addEventListener('change', applyModeVisibility);
    });
    applyModeVisibility();

    formEl.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      const inp = (activeMode() === 'folder') ? folderInput : filesInput;
      if (!inp || !inp.files.length) {
        statusEl.textContent = 'no files selected';
        return;
      }
      const fd = new FormData();
      for (let i = 0; i < inp.files.length; i++) {
        fd.append('files', inp.files[i]);
      }
      statusEl.textContent = 'uploading ' + inp.files.length + ' file(s)...';
      try {
        const result = await jsonFetch(
          '/api/videos', { method: 'POST', body: fd },
        );
        const u = (result.uploaded || []).length;
        const f = (result.failed || []).length;
        const s = (result.skipped || []).length;
        const parts = [u + ' uploaded'];
        if (s) parts.push(s + ' skipped (non-video)');
        if (f) parts.push(f + ' failed');
        statusEl.textContent = parts.join(', ');
        if (f) {
          const detail = result.failed.map(function (x) {
            return '· ' + x.filename + ' — ' + x.error;
          }).join('\n');
          alert('일부 실패:\n' + detail);
        }
        if (u > 0) {
          setTimeout(function () { location.reload(); }, 700);
        }
      } catch (e) {
        statusEl.textContent = 'error: ' + e.message;
      }
    });
  }

  // --- Server-side import ----------------------------------------------
  // For environments where browser file upload is blocked. Operator drops
  // videos into FX_IMPORT_DIR via SCP / network share, then this UI scans
  // and registers them without any multipart traffic.
  function bindImport(els) {
    if (!els || !els.scanBtn) return;
    const { scanBtn, allBtn, selectedBtn, statusEl, container, table, checkAll } = els;
    const tbody = table.querySelector('tbody');
    let lastScan = [];

    function fmtMB(n) {
      if (!n && n !== 0) return '-';
      return (n / 1024 / 1024).toFixed(1) + ' MB';
    }

    function renderRows(files) {
      tbody.innerHTML = '';
      files.forEach(function (f, i) {
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td><input type="checkbox" class="import-pick" data-name="' +
          f.name.replace(/"/g, '&quot;') + '"></td>' +
          '<td class="mono small">' + f.name + '</td>' +
          '<td class="mono">' + f.ext + '</td>' +
          '<td class="mono">' + fmtMB(f.size_bytes) + '</td>';
        tbody.appendChild(tr);
      });
    }

    scanBtn.addEventListener('click', async function () {
      statusEl.textContent = 'scanning...';
      allBtn.disabled = true;
      selectedBtn.disabled = true;
      try {
        const r = await jsonFetch('/api/import');
        lastScan = r.files || [];
        renderRows(lastScan);
        container.style.display = lastScan.length ? '' : 'none';
        statusEl.textContent = lastScan.length
          ? (lastScan.length + ' file(s) found under ' + r.root)
          : ('empty: ' + r.root);
        allBtn.disabled = !lastScan.length;
        selectedBtn.disabled = !lastScan.length;
      } catch (e) {
        statusEl.textContent = 'scan error: ' + e.message;
      }
    });

    if (checkAll) {
      checkAll.addEventListener('change', function () {
        tbody.querySelectorAll('.import-pick').forEach(function (cb) {
          cb.checked = checkAll.checked;
        });
      });
    }

    async function runImport(names) {
      const label = names.length ? (names.length + ' selected') : 'all';
      statusEl.textContent = 'importing ' + label + '...';
      allBtn.disabled = true;
      selectedBtn.disabled = true;
      try {
        const r = await jsonFetch('/api/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ names: names }),
        });
        const u = (r.uploaded || []).length;
        const f = (r.failed || []).length;
        const s = (r.skipped || []).length;
        const parts = [u + ' imported'];
        if (s) parts.push(s + ' skipped');
        if (f) parts.push(f + ' failed');
        statusEl.textContent = parts.join(', ') + (r.moved ? ' (moved)' : ' (copied)');
        if (f) {
          const detail = r.failed.map(function (x) {
            return '· ' + x.filename + ' — ' + x.error;
          }).join('\n');
          alert('일부 실패:\n' + detail);
        }
        if (u > 0) {
          setTimeout(function () { location.reload(); }, 700);
        }
      } catch (e) {
        statusEl.textContent = 'error: ' + e.message;
      } finally {
        allBtn.disabled = !lastScan.length;
        selectedBtn.disabled = !lastScan.length;
      }
    }

    allBtn.addEventListener('click', function () { runImport([]); });
    selectedBtn.addEventListener('click', function () {
      const names = [];
      tbody.querySelectorAll('.import-pick:checked').forEach(function (cb) {
        names.push(cb.dataset.name);
      });
      if (!names.length) {
        statusEl.textContent = 'no rows selected';
        return;
      }
      runImport(names);
    });
  }

  // --- Delete video ----------------------------------------------------
  function bindDeleteVideo() {
    document.querySelectorAll('[data-action="delete-video"]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        if (!confirm('영상과 모든 추출 작업/프레임을 삭제합니다. 계속?')) return;
        const id = btn.dataset.id;
        try {
          await jsonFetch('/api/videos/' + id, { method: 'DELETE' });
          location.reload();
        } catch (e) {
          alert('삭제 실패: ' + e.message);
        }
      });
    });
  }

  // --- Job form --------------------------------------------------------
  function bindJobForm(formEl, statusEl) {
    if (!formEl) return;
    formEl.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      const videoId = formEl.dataset.videoId;
      const fd = new FormData(formEl);
      const body = {};
      // numeric / nullable parsing
      const numFields = [
        'target_fps', 'interval_sec',
        'resize_w', 'resize_h',
        'head_skip_sec', 'tail_skip_sec',
        'random_n', 'seed',
      ];
      fd.forEach(function (v, k) {
        if (numFields.indexOf(k) >= 0) {
          const s = String(v).trim();
          if (s === '') return;          // empty -> omit
          const n = Number(s);
          if (!isNaN(n)) body[k] = n;
        } else {
          body[k] = v;
        }
      });
      body.format = 'png';

      statusEl.textContent = 'submitting...';
      try {
        const job = await jsonFetch(
          '/api/videos/' + videoId + '/jobs',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          },
        );
        statusEl.textContent = 'queued: ' + job.id.slice(0, 8);
        location.reload();
      } catch (e) {
        statusEl.textContent = 'error: ' + e.message;
      }
    });
  }

  // --- Sampling mode toggle -------------------------------------------
  function bindSamplingModeToggle(selectEl) {
    if (!selectEl) return;
    function apply() {
      const mode = selectEl.value;
      document.querySelectorAll('.row-uniform').forEach(function (el) {
        el.style.display = (mode === 'uniform') ? '' : 'none';
      });
      document.querySelectorAll('.row-random').forEach(function (el) {
        el.style.display = (mode === 'random_n') ? '' : 'none';
      });
    }
    selectEl.addEventListener('change', apply);
    apply();
  }

  // --- Cancel job ------------------------------------------------------
  function bindCancelJob() {
    document.querySelectorAll('[data-action="cancel-job"]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        if (!confirm('작업을 취소합니다. 이미 추출된 프레임은 유지됩니다.')) return;
        const id = btn.dataset.id;
        try {
          await jsonFetch('/api/jobs/' + id + '/cancel', { method: 'POST' });
          location.reload();
        } catch (e) {
          alert('취소 실패: ' + e.message);
        }
      });
    });
  }

  // --- Delete frame ----------------------------------------------------
  function bindDeleteFrame() {
    document.querySelectorAll('[data-action="delete-frame"]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        if (!confirm('이 프레임을 삭제합니다.')) return;
        const id = btn.dataset.id;
        try {
          await jsonFetch('/api/frames/' + id, { method: 'DELETE' });
          const fig = btn.closest('figure.thumb');
          if (fig) fig.remove();
        } catch (e) {
          alert('삭제 실패: ' + e.message);
        }
      });
    });
  }

  // --- Job polling -----------------------------------------------------
  function startJobPolling(jobId, isActive) {
    if (!isActive) return;
    const statusEl = document.getElementById('job-status');
    const progressEl = document.getElementById('job-progress');
    const doneEl = document.getElementById('job-done');
    const totalEl = document.getElementById('job-total');

    let stopped = false;
    async function tick() {
      if (stopped) return;
      try {
        const j = await jsonFetch('/api/jobs/' + jobId);
        if (statusEl) {
          statusEl.textContent = j.status;
          statusEl.className = 'mono status status-' + j.status;
        }
        if (progressEl) progressEl.textContent = j.progress_pct;
        if (doneEl) doneEl.textContent = j.frames_done;
        if (totalEl) totalEl.textContent = j.frames_total != null ? j.frames_total : '?';
        if (j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') {
          stopped = true;
          setTimeout(function () { location.reload(); }, 600);
          return;
        }
      } catch (e) {
        // transient — keep polling
      }
      setTimeout(tick, 2000);
    }
    tick();
  }

  // --- Health indicator -----------------------------------------------
  async function refreshHealth() {
    const el = document.getElementById('health-indicator');
    if (!el) return;
    try {
      const h = await jsonFetch('/api/health');
      el.classList.toggle('ok', !!h.ok);
      el.classList.toggle('bad', !h.ok);
      el.title = 'DB ' + (h.ok ? 'OK' : 'FAIL') + ' · ' + h.now;
    } catch (e) {
      el.classList.add('bad');
      el.title = 'health check failed';
    }
  }
  refreshHealth();
  setInterval(refreshHealth, 15000);

  // --- Export ----------------------------------------------------------
  window.Admin = {
    bindUpload: bindUpload,
    bindImport: bindImport,
    bindDeleteVideo: bindDeleteVideo,
    bindJobForm: bindJobForm,
    bindSamplingModeToggle: bindSamplingModeToggle,
    bindCancelJob: bindCancelJob,
    bindDeleteFrame: bindDeleteFrame,
    startJobPolling: startJobPolling,
    bindRescan: bindRescan,
    bindRefreshList: bindRefreshList,
    bindCheckboxSelection: bindCheckboxSelection,
    bindBatchExtract: bindBatchExtract,
  };
})();
