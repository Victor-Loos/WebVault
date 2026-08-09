(() => {
    "use strict";
    const $ = (selector) => document.querySelector(selector);
    const requestedJob = new URLSearchParams(window.location.search).get("job");
    const order = ["queued", "running", "uploading", "completed"];

    function setStages(status) {
        const current = status === "failed" || status === "cancelled" ? "running" : status;
        const currentIndex = order.indexOf(current);
        document.querySelectorAll("[data-stage]").forEach((item) => {
            const index = order.indexOf(item.dataset.stage);
            item.classList.toggle("is-complete", currentIndex >= 0 && index < currentIndex);
            item.classList.toggle("is-current", item.dataset.stage === current);
        });
    }

    async function refresh() {
        try {
            const response = await fetch("/api/jobs", { cache: "no-store" });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Progress is unavailable.");
            const jobs = payload.jobs || [];
            const job = requestedJob
                ? jobs.find((item) => item.job_id === requestedJob)
                : jobs.find((item) => ["queued", "running", "uploading"].includes(item.status));
            if (!job) {
                $("#liveStatus").textContent = "No active crawl";
                $("#liveMessage").textContent = "No running capture was found. The browser view may remain empty until another crawl starts.";
                return;
            }
            $("#liveInfoLink").href = `/captures/${encodeURIComponent(job.job_id)}`;
            const screencast = $("#liveScreencast");
            screencast.hidden = job.status !== "running";
            $("#liveStatus").textContent = job.status;
            $("#liveStatus").className = `status-badge is-${job.status}`;
            setStages(job.status);
            const captured = Number(job.pages_crawled || 0);
            const total = Number(job.pages_total || 0);
            const pending = Number(job.pages_pending || 0);
            $("#liveCaptured").textContent = String(captured);
            $("#livePending").textContent = String(pending);
            $("#liveFailed").textContent = String(Number(job.pages_failed || 0));
            $("#liveProgress").max = Math.max(total, captured, 1);
            $("#liveProgress").value = captured;
            $("#liveProgressPercent").textContent = total ? `${Math.min(100, Math.round((captured / total) * 100))}%` : `${captured} pages`;
            $("#liveProgressLabel").textContent = total ? `${captured} of ${total} discovered pages processed` : "Discovering pages…";
            $("#liveCurrentUrl").textContent = job.current_url || "Waiting for the next page…";
            $("#liveMessage").textContent = job.message || "Capture is running.";
            if (["completed", "failed", "cancelled"].includes(job.status)) {
                $("#liveScreencast").title = "The live browser session has ended";
            }
        } catch (error) {
            $("#liveMessage").textContent = error.message;
        }
    }

    refresh();
    window.setInterval(() => { if (!document.hidden) refresh(); }, 2000);
})();
