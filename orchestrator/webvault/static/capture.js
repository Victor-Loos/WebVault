(() => {
    "use strict";

    const $ = (selector) => document.querySelector(selector);
    const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

    function readableDate(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return value || "Unknown";
        return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", hour12: false }).format(date);
    }

    document.querySelectorAll("time[data-local-date]").forEach((element) => {
        element.textContent = readableDate(element.dateTime);
        element.title = new Date(element.dateTime).toLocaleString(undefined, { dateStyle: "full", timeStyle: "long", hour12: false });
    });
    function bytes(value) {
        const amount = Number(value || 0);
        if (!amount) return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        const index = Math.min(Math.floor(Math.log(amount) / Math.log(1024)), units.length - 1);
        return `${(amount / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    document.querySelectorAll("[data-bytes]").forEach((element) => {
        element.replaceChildren(document.createTextNode(bytes(element.dataset.bytes)));
    });

    async function refreshProgress() {
        const workspace = $(".capture-detail-workspace");
        const statusElement = $("#captureStatus");
        if (!workspace || !statusElement || !["queued", "running", "uploading"].includes(statusElement.textContent.trim())) return;
        try {
            const response = await fetch(`/api/captures/${encodeURIComponent(workspace.dataset.jobId)}`);
            const payload = await response.json();
            if (!response.ok) return;
            const capture = payload.capture;
            const previousStatus = statusElement.textContent.trim();
            statusElement.textContent = capture.status;
            statusElement.className = `status-badge is-${capture.status}`;
            $("#capturePages").textContent = String(capture.pages_crawled || 0);
            $("#captureMessage").textContent = capture.message || "Capture is running.";
            const progress = $("#captureProgress");
            progress.max = Math.max(capture.pages_total || capture.pages_crawled || 1, 1);
            progress.value = capture.pages_crawled || 0;
            if (!["queued", "running", "uploading"].includes(capture.status)) {
                $("#captureLiveProgress").hidden = true;
                if (previousStatus !== capture.status) window.location.reload();
            }
        } catch (_error) {
            // Keep the last known state; the next poll may recover.
        }
    }

    $("#rerunSame")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const feedback = $("#rerunFeedback");
        const jobId = $(".capture-detail-workspace").dataset.jobId;
        button.disabled = true;
        button.textContent = "Queueing…";
        try {
            const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/rerun`, {
                method: "POST",
                headers: { "X-CSRF-Token": csrfToken() },
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Could not queue this version.");
            feedback.hidden = false;
            feedback.textContent = "New version queued. Opening its record…";
            window.setTimeout(() => { window.location.href = `/captures/${encodeURIComponent(payload.job_id)}`; }, 700);
        } catch (error) {
            feedback.hidden = false;
            feedback.textContent = error.message;
            button.disabled = false;
            button.textContent = "New version";
        }
    });

    const managementFeedback = $("#managementFeedback");
    const showManagementFeedback = (message) => {
        managementFeedback.hidden = false;
        managementFeedback.textContent = message;
    };

    async function loadCollections() {
        try {
            const response = await fetch("/api/collections");
            const payload = await response.json();
            if (!response.ok) return;
            const datalist = $("#captureCollections");
            (payload.collections || []).forEach((collection) => {
                const option = document.createElement("option");
                option.value = collection.name;
                datalist.append(option);
            });
        } catch (_error) {
            // The collection can still be entered manually.
        }
    }

    $("#moveCaptureForm")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const button = form.querySelector("button[type='submit']");
        const collection = $("#captureCollectionInput").value.trim();
        if (!collection || collection === form.dataset.collection) {
            showManagementFeedback(collection ? "This capture is already in that collection." : "Enter a collection name.");
            return;
        }
        button.disabled = true;
        button.textContent = "Moving…";
        try {
            const response = await fetch(`/api/files/${encodeURIComponent(form.dataset.collection)}/${encodeURIComponent(form.dataset.filename)}`, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
                body: JSON.stringify({ collection }),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Could not move this capture.");
            showManagementFeedback(`Capture moved to ${payload.to}. Reloading…`);
            window.setTimeout(() => window.location.reload(), 500);
        } catch (error) {
            showManagementFeedback(error.message);
            button.disabled = false;
            button.textContent = "Move version";
        }
    });

    $("#copyCaptureLink")?.addEventListener("click", async (event) => {
        try {
            await navigator.clipboard.writeText(window.location.href);
            event.currentTarget.textContent = "Version link copied";
        } catch (_error) {
            showManagementFeedback("Could not access the clipboard. Copy the address from your browser.");
        }
    });

    $("#deleteCapture")?.addEventListener("click", async (event) => {
        const workspace = $(".capture-detail-workspace");
        if (!window.confirm("Permanently delete this capture and its WACZ archive? Other versions will remain available.")) return;
        const button = event.currentTarget;
        button.disabled = true;
        button.textContent = "Deleting…";
        try {
            const response = await fetch(`/api/captures/${encodeURIComponent(workspace.dataset.jobId)}`, {
                method: "DELETE",
                headers: { "X-CSRF-Token": csrfToken() },
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Could not delete this capture.");
            window.location.href = payload.next_url || "/#library";
        } catch (error) {
            showManagementFeedback(error.message);
            button.disabled = false;
            button.textContent = "Delete version…";
        }
    });

    loadCollections();
    window.setInterval(() => { if (!document.hidden) refreshProgress(); }, 3000);
})();
