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
  };

  let lastLiveStamp = null;
  let activeTab = 'auto';
  let jobId = null;
  let pollTimer = null;
  let shopifyConnected = false;
  let lastMediaKind = null;

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

  function setDownload(el, enabled, href) {
    el.classList.toggle('disabled', !enabled);
    el.href = enabled ? href : '#';
  }

  function renderShopify(status) {
    shopifyConnected = Boolean(status?.connected);
    if (shopifyConnected) {
      els.shopifyStatus.textContent = `Connected · ${status.shop}`;
      els.shopifyHint.textContent = `Destination store for Product Harbor media uploads: ${status.shop}. Scopes: ${(status.required_scopes || []).join(', ')}.`;
      els.shopifyForm.classList.add('hidden');
      els.shopifyDisconnect.classList.remove('hidden');
      els.shopifyToken.value = '';
    } else {
      els.shopifyStatus.textContent = 'Not connected';
      els.shopifyHint.textContent = status?.setup_hint || 'Connect a store to prepare Shopify-ready CSV with CDN URLs.';
      els.shopifyForm.classList.remove('hidden');
      els.shopifyDisconnect.classList.add('hidden');
    }
  }

  async function refreshShopify() {
    try {
      const response = await fetch('/api/shopify/status');
      const payload = await response.json();
      if (payload.ok) renderShopify(payload.shopify);
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
    els.continueBtn.disabled = !(job.state === 'running' && job.interactive_browser);
    els.reasons.innerHTML = '';
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
        }),
      });
      const payload = await response.json();
      if (!payload.ok) {
        els.message.textContent = payload.error || 'Could not start job.';
        setBusy(false);
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
        els.shopifyHint.textContent = payload.error || 'Could not save connection.';
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

  syncInteractiveVisibility();
  refreshShopify();
})();
