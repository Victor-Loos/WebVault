(() => {
    "use strict";
    const $ = (selector) => document.querySelector(selector);
    const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");

    async function refresh() {
        const list = $("#liveTargets");
        try {
            const response = await fetch("/api/live/targets");
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Browser control is unavailable.");
            const host = ["localhost", "127.0.0.1", "::1"].includes(location.hostname) ? location.hostname : "127.0.0.1";
            list.innerHTML = payload.targets.length ? payload.targets.map((target) => {
                const debuggerUrl = `http://${host}:9222/devtools/inspector.html?ws=${host}:9222/devtools/page/${encodeURIComponent(target.id)}`;
                return `<article class="live-target-card"><div><strong>${escapeHtml(target.title)}</strong><small>${escapeHtml(target.url)}</small></div><a class="button button-primary" href="${debuggerUrl}" target="_blank" rel="noopener">Open DevTools</a></article>`;
            }).join("") : '<div class="empty-state"><strong>No controllable tabs found</strong><p>Start a capture with Interactive control enabled, then refresh.</p></div>';
        } catch (error) {
            list.innerHTML = `<div class="empty-state"><strong>Interactive control unavailable</strong><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    $("#refreshTargets").addEventListener("click", refresh);
    refresh();
})();
