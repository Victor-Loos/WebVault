(() => {
    "use strict";

    const $ = (selector) => document.querySelector(selector);
    let activeJobId = "";
    let pollTimer = null;

    function escapeHtml(value) {
        return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
    }

    function bytes(value) {
        const amount = Number(value || 0);
        if (!amount) return "0 B";
        const units = ["B", "KB", "MB", "GB"];
        const index = Math.min(Math.floor(Math.log(amount) / Math.log(1024)), units.length - 1);
        return `${(amount / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    async function api(path, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
            options.headers = { ...(options.headers || {}), "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content };
        }
        const response = await fetch(path, options);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "Request failed.");
        return payload;
    }

    async function loadProfiles() {
        const payload = await api("/api/profiles");
        $("#profileList").innerHTML = payload.profiles.length
            ? payload.profiles.map((profile) => `<article class="profile-list-item"><div><strong>${escapeHtml(profile.name)}</strong><small>${bytes(profile.size_bytes)} · ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(profile.created_at))}</small></div><a class="button button-quiet" href="/?profile=${encodeURIComponent(profile.id)}#capture">Use profile</a></article>`).join("")
            : '<div class="empty-state compact-empty"><strong>No login profiles yet</strong><p>Create one for a site that requires authentication.</p></div>';
    }

    async function pollJob() {
        if (!activeJobId) return;
        try {
            const payload = await api("/api/jobs");
            const job = payload.jobs.find((item) => item.job_id === activeJobId);
            if (!job) return;
            $("#profileSessionTitle").textContent = job.status === "running" ? "Browser ready for sign-in" : job.status === "completed" ? "Profile saved" : job.status === "failed" ? "Profile setup failed" : "Preparing the browser…";
            $("#profileSessionMessage").textContent = job.message || "Waiting for an update…";
            $("#openProfileBrowser").classList.toggle("is-disabled", job.status !== "running");
            if (["completed", "failed", "cancelled"].includes(job.status)) {
                window.clearTimeout(pollTimer);
                if (job.status === "completed") await loadProfiles();
                return;
            }
        } catch (_error) {
            // Keep polling while the worker transitions.
        }
        pollTimer = window.setTimeout(pollJob, 2000);
    }

    $("#profileForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = $("#profileSubmit");
        button.disabled = true;
        try {
            const payload = await api("/api/profiles", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: $("#profileName").value, url: $("#profileUrl").value }),
            });
            activeJobId = payload.job_id;
            $("#profileSession").hidden = false;
            pollJob();
        } catch (error) {
            $("#profileSession").hidden = false;
            $("#profileSessionTitle").textContent = "Could not start profile setup";
            $("#profileSessionMessage").textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    $("#cancelProfile").addEventListener("click", async () => {
        if (!activeJobId) return;
        await api(`/api/jobs/${encodeURIComponent(activeJobId)}/cancel`, { method: "POST" });
        $("#profileSessionMessage").textContent = "Cancellation requested…";
    });

    loadProfiles().catch((error) => { $("#profileList").textContent = error.message; });
})();
