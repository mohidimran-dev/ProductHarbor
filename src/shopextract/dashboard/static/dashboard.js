(() => {
  const NOTES = {
    auto: 'Auto Detect finds the platform, then routes to the matching Product Harbor extractor.',
    shopify: 'Shopify uses the public products.json / collection API path.',
    woocommerce: 'WooCommerce uses the Store API extractor.',
    custom: 'Custom / Other uses the generic crawl path with browser support. Enable interactive headed browser for protected storefronts.',
  };

  const els = {
    tabs: [...document.querySelectorAll('.tab')],
    url: document.getElementById('url'),
    start: document.getElementById('start'),
    continueBtn: document.getElementById('continue'),
    cancel: document.getElementById('cancel'),
    interactive: document.getElementById('interactive'),
    interactiveWrap: document.getElementById('interactive-wrap'),
    humanGate: document.getElementById('human-gate'),
    humanGateTitle: document.getElementById('human-gate-title'),
    humanGateSteps: document.getElementById('human-gate-steps'),
    note: document.getElementById('backend-note'),
    state: document.getElementById('state'),
    elapsed: document.getElementById('elapsed'),
    platform: document.getElementById('platform'),
    completeness: document.getElementById('completeness'),
    httpPages: document.getElementById('http-pages'),
    discovered: document.getElementById('discovered'),
    extracted: document.getElementById('extracted'),
    failed: document.getElementById('failed'),
    duplicates: document.getElementById('duplicates'),
    pending: document.getElementById('pending'),
    variants: document.getElementById('variants'),
    runLimitNote: document.getElementById('run-limit-note'),
    message: document.getElementById('message'),
    reasons: document.getElementById('reasons'),
    preview: document.getElementById('preview'),
    downloadProducts: document.getElementById('download-products'),
    downloadReport: document.getElementById('download-report'),
    downloadShopify: document.getElementById('download-shopify'),
    livePanel: document.getElementById('live-panel'),
    liveLabel: document.getElementById('live-label'),
    liveImage: document.getElementById('live-image'),
    liveEmpty: document.getElementById('live-empty'),
    shopifyStatus: document.getElementById('shopify-status'),
    shopifyHint: document.getElementById('shopify-hint'),
    shopifyForm: document.getElementById('shopify-connect-form'),
    shopifyShop: document.getElementById('shopify-shop'),
    shopifyToken: document.getElementById('shopify-token'),
    shopifySave: document.getElementById('shopify-save'),
    shopifyDisconnect: document.getElementById('shopify-disconnect'),
    shopifyLive: document.getElementById('shopify-live-upload'),
    shopifyLiveStatus: document.getElementById('shopify-live-status'),
    shopifyCsvImport: document.getElementById('shopify-csv-import'),
    shopifyCsvFile: document.getElementById('shopify-csv-file'),
    shopifyCsvCancel: document.getElementById('shopify-csv-cancel'),
    shopifyCsvStore: document.getElementById('shopify-csv-store'),
    shopifyCsvStatus: document.getElementById('shopify-csv-status'),
    shopifyCsvResults: document.getElementById('shopify-csv-results'),
    mediaZip: document.getElementById('media-zip'),
    mediaShopify: document.getElementById('media-shopify'),
    mediaCancel: document.getElementById('media-cancel'),
    mediaResume: document.getElementById('media-resume'),
    mediaMessage: document.getElementById('media-message'),
    mediaTotal: document.getElementById('media-total'),
    mediaDownloaded: document.getElementById('media-downloaded'),
    mediaUploaded: document.getElementById('media-uploaded'),
    mediaFailed: document.getElementById('media-failed'),
    downloadZip: document.getElementById('download-zip'),
    downloadShopifyReady: document.getElementById('download-shopify-ready'),
    downloadMediaFailure: document.getElementById('download-media-failure'),
    scheduleDaily: document.getElementById('schedule-daily'),
    cooldownStatus: document.getElementById('cooldown-status'),
  };

  let lastLiveStamp = null;
  let activeTab = 'auto';
  let jobId = null;
  let lastJob = null;
  let pollTimer = null;
  let shopifyConnected = false;
  let liveUpload = false;
  let lastMediaKind = null;
  let csvImportId = null;
  let csvPollTimer = null;

  function selectedMode() {
    return document.querySelector('input[name="mode"]:checked')?.value || 'test';
  }

  function setBusy(busy) {
    els.start.disabled = busy;
    els.cancel.disabled = !busy;
  }

  function syncInteractiveVisibility() {
    const show = activeTab === 'custom';
    els.interactiveWrap.classList.toggle('hidden', !show);
    if (!show) els.interactive.checked = false;
  }

  function pointNext(job) {
    const banner = document.getElementById('next-step');
    const buttons = [els.shopifySave, els.shopifyLive, els.start, els.continueBtn].filter(Boolean);
    const mark = (target) => {
      for (const button of buttons) {
        const on = button === target;
        button.classList.toggle('primary', on);
        button.classList.toggle('ghost', !on);
      }
    };
    const say = (text) => {
      if (banner) banner.textContent = text;
    };
    const running = Boolean(job && (job.state === 'running' || job.state === 'queued'));
    const awaiting = Boolean(job && job.state === 'running' && job.awaiting_human);
    if (!shopifyConnected) {
      mark(els.shopifySave);
      say('Step 1. Enter the store domain and Admin API token, then press Save connection.');
      return;
    }
    if (awaiting) {
      mark(els.continueBtn);
      say('The Chromium window needs you. Finish that page, then press Continue. An access denial cannot be clicked through.');
      return;
    }
    if (running) {
      mark(null);
      say('The scrape is running. Leave this page open. Product cards appear in step 4 as they are admitted.');
      return;
    }
    if (job && job.state === 'blocked') {
      mark(null);
      say('This run stopped. Read the message in step 3. Continue cannot open a page the store has denied.');
      return;
    }
    if (job && ['succeeded', 'partial'].includes(job.state)) {
      mark(null);
      say('Review the product cards in step 4. A product already in the store was skipped, not created again.');
      return;
    }
    if (!liveUpload) {
      mark(els.shopifyLive);
      say('Step 2. Press Upload each scraped product. After that, paste a collection URL and press Start scrape.');
      return;
    }
    mark(els.start);
    say('Step 3. Paste one collection URL and press Start scrape. Each new product is created in the store as it is admitted.');
  }

  function setDownload(el, enabled, href) {
    el.classList.toggle('disabled', !enabled);
    el.href = enabled ? href : '#';
  }

  function renderShopify(status) {
    shopifyConnected = Boolean(status?.connected);
    liveUpload = Boolean(status?.live_upload) && shopifyConnected;
    if (els.shopifyLive) {
      els.shopifyLive.disabled = !shopifyConnected;
      els.shopifyLive.textContent = liveUpload
        ? 'Stop uploading scraped products'
        : 'Upload each scraped product';
    }
    if (shopifyConnected) {
      const shopLabel = status.shop_name ? `${status.shop_name} (${status.shop})` : status.shop;
      els.shopifyStatus.textContent = `Connected · ${shopLabel}`;
      const granted = status.granted_scopes || [];
      const missing = status.missing_scopes || [];
      if (missing.length) {
        els.shopifyHint.textContent = `Missing scopes: ${missing.join(', ')}. ${status.reauthorize_hint || 'Reinstall the app for a new token, then reconnect.'}`;
      } else if (granted.length) {
        els.shopifyHint.textContent = `Granted scopes: ${granted.join(', ')}.`;
      } else {
        els.shopifyHint.textContent = `Store ${status.shop}. Required scopes: ${(status.required_scopes || []).join(', ')}.`;
      }
      if (status.verify_error) {
        els.shopifyHint.textContent = `${status.verify_error} ${status.reauthorize_hint || ''}`.trim();
      }
      els.shopifyForm.classList.add('hidden');
      els.shopifyDisconnect.classList.remove('hidden');
      els.shopifyToken.value = '';
      if (els.shopifyCsvImport) els.shopifyCsvImport.disabled = Boolean(missing.length);
      if (els.shopifyCsvStore) {
        els.shopifyCsvStore.textContent = missing.length
          ? `CSV import blocked until write_files is on the token for ${status.shop}.`
          : `CSV imports go to ${status.shop}. Products are created as drafts after their images are ready in Shopify Files.`;
      }
    } else {
      els.shopifyStatus.textContent = 'Not connected';
      els.shopifyHint.textContent = 'The token needs write_products, read_products, write_files, and read_files. It stays on this machine.';
      els.shopifyForm.classList.remove('hidden');
      els.shopifyDisconnect.classList.add('hidden');
      liveUpload = false;
      if (els.shopifyCsvImport) els.shopifyCsvImport.disabled = true;
      if (els.shopifyCsvStore) {
        els.shopifyCsvStore.textContent = 'Connect a store to import a product CSV into Shopify.';
      }
    }
    if (els.shopifyLiveStatus && !els.shopifyLiveStatus.dataset.busy) {
      els.shopifyLiveStatus.textContent = liveUpload
        ? 'On. Each new scraped product is created in this store. Products already uploaded are skipped.'
        : shopifyConnected
          ? 'Press the orange button to turn upload on, then start the scrape.'
          : 'Connect the store, then turn this on before you start a scrape.';
    }
    pointNext(lastJob);
  }

  function renderCsvImport(job) {
    if (!job || !els.shopifyCsvStatus) return;
    csvImportId = job.job_id;
    const running = job.state === 'running' || job.state === 'queued';
    if (els.shopifyCsvCancel) {
      els.shopifyCsvCancel.classList.toggle('hidden', !running);
      els.shopifyCsvCancel.disabled = !running;
    }
    if (els.shopifyCsvImport) els.shopifyCsvImport.disabled = running || !shopifyConnected;
    if (els.shopifyCsvStore && job.shop) {
      els.shopifyCsvStore.textContent = `Importing into ${job.shop} from ${job.filename || 'CSV'}.`;
    }
    els.shopifyCsvStatus.textContent = job.message || job.progress_label || 'CSV import running…';
    if (!els.shopifyCsvResults) return;
    els.shopifyCsvResults.innerHTML = '';
    for (const item of job.results || []) {
      const li = document.createElement('li');
      if (item.store_level) {
        li.textContent = item.reason || job.message || 'Shopify Files access denied for this store.';
        els.shopifyCsvResults.appendChild(li);
        continue;
      }
      const rows = (item.source_rows || []).join(', ');
      const network = item.skip_kind === 'network';
      const prefix = item.status === 'imported'
        ? 'Imported'
        : (item.status === 'failed' ? 'Failed' : (network ? 'Network skip' : 'Skipped'));
      const handle = item.handle || 'product';
      let text = `${prefix} ${handle}`;
      if (rows) text += ` (CSV row ${rows})`;
      if (item.reason) text += ` — ${item.reason}`;
      if (network) li.classList.add('network-skip');
      if (item.admin_url) {
        li.textContent = `${text} `;
        const link = document.createElement('a');
        link.href = item.admin_url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = 'Open in Shopify';
        li.appendChild(link);
      } else {
        li.textContent = text;
      }
      els.shopifyCsvResults.appendChild(li);
    }
  }

  async function pollCsvImport() {
    if (!csvImportId) return;
    try {
      const response = await fetch(`/api/shopify/csv-import/${csvImportId}`);
      const payload = await response.json();
      if (!payload.ok || !payload.import) return;
      renderCsvImport(payload.import);
      if (['succeeded', 'partial', 'failed', 'cancelled'].includes(payload.import.state)) {
        clearInterval(csvPollTimer);
        csvPollTimer = null;
      }
    } catch (_error) {
      // Keep the last progress text if a poll fails.
    }
  }

  function ensureCsvPolling() {
    if (csvPollTimer) return;
    csvPollTimer = setInterval(pollCsvImport, 900);
  }

  function renderCooldown(cooling) {
    if (!els.cooldownStatus) return;
    if (cooling && cooling.active) {
      els.cooldownStatus.textContent = cooling.message || 'A denial cooldown is active.';
      els.start.disabled = true;
    } else {
      els.cooldownStatus.textContent = cooling?.message
        ? ''
        : 'Batch mode opens up to 15 products, waits 20–60 seconds between pages, and skips families already in Shopify.';
    }
  }

  async function refreshRunControl() {
    try {
      const response = await fetch('/api/run-control');
      const payload = await response.json();
      if (payload.ok) renderCooldown(payload.run_control?.cooldown);
    } catch (_error) {
      // Status strip is optional; start still checks the server.
    }
  }

  async function refreshShopify() {
    try {
      const response = await fetch('/api/shopify/status');
      const payload = await response.json();
      if (payload.ok) {
        renderShopify(payload.shopify);
        if (payload.cooldown) renderCooldown(payload.cooldown);
      }
    } catch (error) {
      els.shopifyStatus.textContent = 'Status unavailable';
      els.shopifyHint.textContent = error.message;
    }
  }

  function renderMedia(job) {
    const hasProducts = Boolean(job?.downloads?.products_json);
    const media = job?.media;
    const mediaRunning = media && media.state === 'running';
    const canStart = hasProducts && !mediaRunning && ['succeeded', 'partial'].includes(job?.state);
    els.mediaZip.disabled = !canStart;
    els.mediaShopify.disabled = !canStart || !shopifyConnected;
    els.mediaCancel.disabled = !mediaRunning;
    els.mediaResume.disabled = !(media && media.resumable && !mediaRunning);

    if (!hasProducts) {
      els.mediaMessage.textContent = 'Run an extraction with products first.';
    } else if (media) {
      const dest = media.shop ? ` → ${media.shop}` : '';
      els.mediaMessage.textContent = `${media.message || media.state}${dest}`;
      lastMediaKind = media.kind || lastMediaKind;
    } else {
      els.mediaMessage.textContent = shopifyConnected
        ? 'Ready. Download a backup ZIP, or prepare a Shopify-ready CSV (uploads images to your store Files library).'
        : 'Ready for ZIP backup. Connect Shopify to prepare a CDN-mapped CSV.';
    }

    const download = media?.download || {};
    const upload = media?.upload || {};
    els.mediaTotal.textContent = String(download.total ?? 0);
    els.mediaDownloaded.textContent = String(download.downloaded ?? 0);
    els.mediaUploaded.textContent = String(upload.uploaded ?? 0);
    const failed = Number(download.failed || 0) + Number(upload.failed || 0);
    els.mediaFailed.textContent = String(failed);

    const id = job?.job_id;
    setDownload(els.downloadZip, Boolean(job?.downloads?.csv_images_zip || media?.downloads?.csv_images_zip), id ? `/api/jobs/${id}/download/csv_images_zip` : '#');
    setDownload(els.downloadShopifyReady, Boolean(job?.downloads?.shopify_ready_csv || media?.downloads?.shopify_ready_csv), id ? `/api/jobs/${id}/download/shopify_ready` : '#');
    setDownload(els.downloadMediaFailure, Boolean(job?.downloads?.media_failure_report || media?.downloads?.failure_report), id ? `/api/jobs/${id}/download/media_failure_report` : '#');
  }

  function renderJob(job) {
    if (!job) return;
    lastJob = job;
    els.state.textContent = job.state;
    els.state.className = `value state-${job.state}`;
    els.elapsed.textContent = `${Number(job.elapsed_seconds || 0).toFixed(1)}s`;
    els.platform.textContent = job.platform || '—';
    if (job.catalog_complete === true) els.completeness.textContent = 'confirmed complete';
    else if (job.catalog_complete === false) els.completeness.textContent = 'incomplete / partial';
    else els.completeness.textContent = 'unverified';
    const fmt = (value) => (value === null || value === undefined ? '—' : String(value));
    els.httpPages.textContent = fmt(job.counts?.http_pages);
    els.discovered.textContent = fmt(job.counts?.discovered);
    els.extracted.textContent = fmt(job.counts?.extracted);
    els.failed.textContent = fmt(job.counts?.failed);
    els.duplicates.textContent = fmt(job.counts?.duplicates_merged);
    els.pending.textContent = fmt(job.counts?.pending);
    els.variants.textContent = String(job.counts?.variants ?? 0);
    els.runLimitNote?.classList.toggle('hidden', !job.limited_run);
    els.message.textContent = job.message || '';
    const interactiveRunning = job.state === 'running' && job.interactive_browser;
    const awaiting = Boolean(job.awaiting_human);
    els.continueBtn.disabled = !interactiveRunning;
    els.continueBtn.textContent = (job.human_action && job.human_action.continue_label)
      || 'Continue';
    const action = job.human_action;
    const showGate = Boolean(
      action
      && job.interactive_browser
      && (awaiting || job.state === 'blocked' || job.state === 'running'),
    );
    els.humanGate.classList.toggle('hidden', !showGate);
    if (showGate) {
      els.humanGateTitle.textContent = action.title || 'Human step required';
      els.humanGateSteps.innerHTML = '';
      for (const step of action.steps || []) {
        const li = document.createElement('li');
        li.textContent = step;
        els.humanGateSteps.appendChild(li);
      }
    }
    els.reasons.innerHTML = '';
    if (job.duplicate_report) {
      const li = document.createElement('li');
      li.textContent = job.duplicate_report;
      els.reasons.appendChild(li);
    }
    for (const reason of job.incompleteness_reasons || []) {
      const li = document.createElement('li');
      li.textContent = reason;
      els.reasons.appendChild(li);
    }
    for (const error of job.errors || []) {
      const li = document.createElement('li');
      li.textContent = error;
      els.reasons.appendChild(li);
    }

    setDownload(els.downloadProducts, Boolean(job.downloads?.products_json), `/api/jobs/${job.job_id}/download/products`);
    setDownload(els.downloadReport, Boolean(job.downloads?.report_json), `/api/jobs/${job.job_id}/download/report`);
    setDownload(els.downloadShopify, Boolean(job.downloads?.shopify_csv), `/api/jobs/${job.job_id}/download/shopify`);

    const showLive = Boolean(job.interactive_browser);
    els.livePanel.classList.toggle('hidden', !showLive);
    if (showLive) {
      const live = job.live_preview || {};
      if (live.available) {
        els.liveEmpty.hidden = true;
        els.liveImage.hidden = false;
        const stamp = live.updated_at || '';
        if (stamp !== lastLiveStamp) {
          lastLiveStamp = stamp;
          els.liveImage.src = `/api/jobs/${job.job_id}/live-preview?t=${encodeURIComponent(stamp)}`;
        }
        els.liveLabel.textContent = live.label
          ? `Browser snapshot: ${live.label}`
          : 'Live browser snapshot';
      } else {
        els.liveImage.hidden = true;
        els.liveEmpty.hidden = false;
        els.liveEmpty.textContent = job.state === 'running'
          ? 'Waiting for first browser snapshot…'
          : 'No browser snapshot captured.';
      }
    }

    renderMedia(job);
    const upload = job.upload || {};
    if (els.shopifyLiveStatus && (upload.enabled || upload.queued || upload.uploaded || upload.failed || upload.skipped)) {
      els.shopifyLiveStatus.dataset.busy = '1';
      const parts = [
        upload.enabled ? 'Live upload on' : 'Live upload off',
        `queued ${upload.queued || 0}`,
        `uploaded ${upload.uploaded || 0}`,
        `skipped ${upload.skipped || 0}`,
        `failed ${upload.failed || 0}`,
      ];
      els.shopifyLiveStatus.textContent = `${parts.join(' · ')}. ${upload.message || ''}`.trim();
    }
    pointNext(job);

    const preview = job.preview || [];
    if (!preview.length) {
      els.preview.className = 'preview empty';
      els.preview.textContent = job.state === 'running' ? 'Waiting for admitted products…' : 'No products yet.';
      return;
    }
    els.preview.className = 'preview';
    els.preview.innerHTML = '';
    for (const item of preview) {
      const card = document.createElement('article');
      card.className = 'card';
      if (item.image_url) {
        const img = document.createElement('img');
        img.alt = item.title || 'Product';
        img.src = item.image_url;
        img.loading = 'lazy';
        card.appendChild(img);
      } else {
        const placeholder = document.createElement('div');
        placeholder.style.cssText = 'aspect-ratio:1;background:#2a2724;';
        card.appendChild(placeholder);
      }
      const meta = document.createElement('div');
      meta.className = 'meta';
      const title = document.createElement('h3');
      title.textContent = item.title || 'Untitled';
      const price = document.createElement('p');
      price.textContent = [item.price, item.currency].filter(Boolean).join(' ') || 'Price unavailable';
      const variants = document.createElement('p');
      variants.textContent = `${item.variant_count || 0} variants`;
      meta.append(title, price, variants);
      card.appendChild(meta);
      els.preview.appendChild(card);
    }
  }

  async function poll() {
    if (!jobId) return;
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      const payload = await response.json();
      if (!payload.ok) {
        els.message.textContent = payload.error || 'Job status unavailable.';
        return;
      }
      renderJob(payload.job);
      const mediaRunning = payload.job.media && payload.job.media.state === 'running';
      const terminal = ['succeeded', 'failed', 'cancelled', 'blocked', 'partial'].includes(payload.job.state);
      if (terminal && !mediaRunning) {
        clearInterval(pollTimer);
        pollTimer = null;
        setBusy(false);
      } else if (mediaRunning && !pollTimer) {
        pollTimer = setInterval(poll, 700);
      }
    } catch (error) {
      els.message.textContent = `Status poll failed: ${error.message}`;
    }
  }

  function ensurePolling() {
    if (!pollTimer) pollTimer = setInterval(poll, 700);
  }

  async function startMedia(kind, resume = false) {
    if (!jobId) return;
    lastMediaKind = kind;
    els.mediaMessage.textContent = kind === 'zip'
      ? 'Starting Product Harbor CSV + Images ZIP…'
      : 'Starting Product Harbor Shopify-ready prepare…';
    try {
      const response = await fetch(`/api/jobs/${jobId}/media`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, resume }),
      });
      const payload = await response.json();
      if (!payload.ok) {
        els.mediaMessage.textContent = payload.error || 'Could not start media job.';
        if (payload.shopify) renderShopify(payload.shopify);
        return;
      }
      if (payload.destination_shop) {
        els.mediaMessage.textContent = `Destination store: ${payload.destination_shop}. ${payload.media?.message || ''}`.trim();
      }
      renderJob(payload.job);
      ensurePolling();
      poll();
    } catch (error) {
      els.mediaMessage.textContent = `Media start failed: ${error.message}`;
    }
  }

  els.tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      activeTab = tab.dataset.tab;
      els.tabs.forEach((button) => {
        const on = button === tab;
        button.classList.toggle('active', on);
        button.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      els.note.textContent = NOTES[activeTab];
      syncInteractiveVisibility();
    });
  });

  els.start.addEventListener('click', async () => {
    const url = els.url.value.trim();
    if (!url) {
      els.message.textContent = 'Enter a collection URL.';
      return;
    }
    setBusy(true);
    lastLiveStamp = null;
    els.message.textContent = 'Starting job…';
    els.reasons.innerHTML = '';
    els.livePanel.classList.toggle('hidden', !(activeTab === 'custom' && els.interactive.checked));
    els.liveImage.hidden = true;
    els.liveEmpty.hidden = false;
    els.liveEmpty.textContent = 'Waiting for first browser snapshot…';
    try {
      const response = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url,
          tab: activeTab,
          mode: selectedMode(),
          interactive_browser: Boolean(els.interactive.checked && activeTab === 'custom'),
          upload_products: liveUpload,
          schedule_daily: Boolean(els.scheduleDaily?.checked),
        }),
      });
      const payload = await response.json();
      if (!payload.ok) {
        els.message.textContent = payload.error || 'Could not start job.';
        setBusy(false);
        refreshRunControl();
        return;
      }
      jobId = payload.job.job_id;
      renderJob(payload.job);
      clearInterval(pollTimer);
      pollTimer = setInterval(poll, 700);
      poll();
    } catch (error) {
      els.message.textContent = `Start failed: ${error.message}`;
      setBusy(false);
    }
  });

  els.continueBtn.addEventListener('click', async () => {
    if (!jobId) return;
    try {
      const response = await fetch(`/api/jobs/${jobId}/continue`, { method: 'POST' });
      const payload = await response.json();
      if (!payload.ok) {
        els.message.textContent = payload.error || 'Continue failed.';
        return;
      }
      renderJob(payload.job);
    } catch (error) {
      els.message.textContent = `Continue failed: ${error.message}`;
    }
  });

  els.cancel.addEventListener('click', async () => {
    if (!jobId) return;
    try {
      const response = await fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
      const payload = await response.json();
      if (payload.ok) renderJob(payload.job);
    } catch (error) {
      els.message.textContent = `Cancel failed: ${error.message}`;
    }
  });

  els.shopifySave.addEventListener('click', async () => {
    try {
      const response = await fetch('/api/shopify/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          shop: els.shopifyShop.value.trim(),
          access_token: els.shopifyToken.value.trim(),
        }),
      });
      const payload = await response.json();
      if (!payload.ok) {
        const hint = [payload.error, payload.reauthorize_hint || payload.shopify?.reauthorize_hint]
          .filter(Boolean)
          .join(' ');
        els.shopifyHint.textContent = hint || 'Could not save connection.';
        if (payload.shopify) renderShopify(payload.shopify);
        return;
      }
      renderShopify(payload.shopify);
      if (jobId) poll();
    } catch (error) {
      els.shopifyHint.textContent = error.message;
    }
  });

  els.shopifyDisconnect.addEventListener('click', async () => {
    try {
      const response = await fetch('/api/shopify/disconnect', { method: 'POST' });
      const payload = await response.json();
      if (payload.ok) renderShopify(payload.shopify);
    } catch (error) {
      els.shopifyHint.textContent = error.message;
    }
  });

  els.shopifyLive.addEventListener('click', async () => {
    try {
      const response = await fetch('/api/shopify/live-upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !liveUpload, job_id: jobId }),
      });
      const payload = await response.json();
      if (!payload.ok) {
        els.shopifyLiveStatus.textContent = payload.error || 'Could not change product upload.';
        return;
      }
      delete els.shopifyLiveStatus.dataset.busy;
      renderShopify(payload.shopify);
    } catch (error) {
      els.shopifyLiveStatus.textContent = error.message;
    }
  });

  els.mediaZip.addEventListener('click', () => startMedia('zip', false));
  els.mediaShopify.addEventListener('click', () => startMedia('shopify_ready', false));
  els.mediaResume.addEventListener('click', () => startMedia(lastMediaKind || 'zip', true));
  els.mediaCancel.addEventListener('click', async () => {
    if (!jobId) return;
    try {
      const response = await fetch(`/api/jobs/${jobId}/media/cancel`, { method: 'POST' });
      const payload = await response.json();
      if (payload.ok) renderJob(payload.job);
      else els.mediaMessage.textContent = payload.error || 'Cancel failed.';
    } catch (error) {
      els.mediaMessage.textContent = error.message;
    }
  });

  if (els.shopifyCsvImport && els.shopifyCsvFile) {
    els.shopifyCsvImport.addEventListener('click', () => {
      if (!shopifyConnected) {
        if (els.shopifyCsvStatus) els.shopifyCsvStatus.textContent = 'Connect a Shopify store first.';
        return;
      }
      els.shopifyCsvFile.value = '';
      els.shopifyCsvFile.click();
    });
    els.shopifyCsvFile.addEventListener('change', async () => {
      const file = els.shopifyCsvFile.files && els.shopifyCsvFile.files[0];
      if (!file) return;
      if (els.shopifyCsvStatus) {
        els.shopifyCsvStatus.textContent = `Uploading ${file.name}…`;
      }
      if (els.shopifyCsvResults) els.shopifyCsvResults.innerHTML = '';
      const body = new FormData();
      body.append('file', file, file.name);
      try {
        const response = await fetch('/api/shopify/csv-import', { method: 'POST', body });
        const raw = await response.text();
        let payload = null;
        try {
          payload = raw ? JSON.parse(raw) : null;
        } catch (_parseError) {
          if (els.shopifyCsvStatus) {
            const hint = /maximum request body size/i.test(raw)
              ? 'CSV is too large for the server upload limit. Restart Product Harbor after the size fix, then try again.'
              : (raw || `HTTP ${response.status}`).slice(0, 240);
            els.shopifyCsvStatus.textContent = `CSV import failed: ${hint}`;
          }
          return;
        }
        if (!payload?.ok) {
          if (els.shopifyCsvStatus) {
            els.shopifyCsvStatus.textContent = payload?.error || 'Could not start CSV import.';
          }
          return;
        }
        renderCsvImport(payload.import);
        ensureCsvPolling();
        pollCsvImport();
      } catch (error) {
        if (els.shopifyCsvStatus) {
          els.shopifyCsvStatus.textContent = `CSV import failed: ${error.message}`;
        }
      }
    });
  }

  if (els.shopifyCsvCancel) {
    els.shopifyCsvCancel.addEventListener('click', async () => {
      if (!csvImportId) return;
      try {
        const response = await fetch(`/api/shopify/csv-import/${csvImportId}/cancel`, { method: 'POST' });
        const payload = await response.json();
        if (payload.ok) renderCsvImport(payload.import);
      } catch (error) {
        if (els.shopifyCsvStatus) els.shopifyCsvStatus.textContent = error.message;
      }
    });
  }

  syncInteractiveVisibility();
  refreshShopify();
  refreshRunControl();
  fetch('/api/jobs/latest')
    .then((response) => response.json())
    .then((payload) => {
      if (!payload.ok || !payload.job || jobId) return;
      // Only resume an in-progress run. Finished/partial scrapes must not fill the
      // workspace until the user starts a new URL scrape.
      const active = new Set(['queued', 'running', 'awaiting_human']);
      if (!active.has(payload.job.state)) return;
      jobId = payload.job.job_id;
      renderJob(payload.job);
    })
    .catch(() => {});
  fetch('/api/shopify/csv-import/latest')
    .then((response) => response.json())
    .then((payload) => {
      if (!payload.ok || !payload.import) return;
      renderCsvImport(payload.import);
      if (payload.import.state === 'running' || payload.import.state === 'queued') {
        ensureCsvPolling();
      }
    })
    .catch(() => {});
})();
