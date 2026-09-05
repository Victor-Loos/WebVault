(() => {
    "use strict";

    const $ = (selector) => document.querySelector(selector);
    let refreshing = false;
    let refreshTimer = null;

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }

    function bytes(value) {
        const amount = Number(value || 0);
        if (!amount) return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        const index = Math.min(Math.floor(Math.log(amount) / Math.log(1024)), units.length - 1);
        return `${(amount / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    function duration(totalSeconds) {
        const seconds = Math.max(Number(totalSeconds || 0), 0);
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
    }

    function percentage(used, total) {
        return total ? Math.round((used / total) * 100) : null;
    }

    function serviceStateClass(service) {
        if (service.status === "available" || service.status === "healthy") {
            return service.state === "busy" ? "is-busy" : "is-ready";
        }
        return "is-unavailable";
    }

    function renderServiceHealth(payload) {
        const services = [
            ["Garage", payload.archive],
            ["Worker", payload.worker],
            ["Browsertrix", payload.browsertrix],
        ];
        $("#serviceHealthList").innerHTML = services.map(([name, service]) => `
            <div class="service-health-item ${serviceStateClass(service)}">
                <span class="status-dot" aria-hidden="true"></span>
                <span><strong>${name}</strong><small>${escapeHtml(service.state)} · ${escapeHtml(service.detail)}</small></span>
            </div>
        `).join("");
    }

    function scheduleRefresh() {
        window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(() => {
            if (document.hidden) scheduleRefresh();
            else refresh(true);
        }, 10000);
    }

    async function refresh(scheduleNext = false) {
        if (refreshing) {
            if (scheduleNext) scheduleRefresh();
            return;
        }
        refreshing = true;
        const button = $("#refreshStats");
        button.disabled = true;
        try {
            const response = await fetch("/api/stats");
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "Statistics are unavailable.");

            $("#statUptime").textContent = duration(payload.process_uptime_seconds);
            $("#statLoad").textContent = payload.load_average.one_minute.toFixed(2);
            $("#statCpu").textContent = `1 / 5 / 15 min: ${payload.load_average.one_minute} · ${payload.load_average.five_minutes} · ${payload.load_average.fifteen_minutes} · ${payload.cpu_count} CPUs`;

            const memoryPercent = percentage(payload.memory.used_bytes, payload.memory.limit_bytes);
            $("#statMemory").textContent = memoryPercent === null ? bytes(payload.memory.used_bytes) : `${memoryPercent}%`;
            $("#statMemoryDetail").textContent = payload.memory.limit_bytes ? `${bytes(payload.memory.used_bytes)} of ${bytes(payload.memory.limit_bytes)}` : `${bytes(payload.memory.used_bytes)} in use`;

            const diskPercent = percentage(payload.disk.used_bytes, payload.disk.total_bytes);
            $("#statDisk").textContent = `${diskPercent ?? 0}%`;
            $("#statDiskDetail").textContent = `${bytes(payload.disk.free_bytes)} free of ${bytes(payload.disk.total_bytes)}`;

            $("#statArchive").textContent = bytes(payload.archive.bytes);
            const capacity = payload.archive.capacity_bytes;
            const remaining = payload.archive.remaining_bytes;
            $("#statArchiveDetail").textContent = capacity
                ? `${bytes(remaining)} remaining of ${bytes(capacity)} · ${payload.archive.files} versions`
                : `${payload.archive.files} versions in ${payload.archive.collections} collections`;
            $("#statJobTotal").textContent = String(payload.jobs.total);
            const workerAge = payload.worker.heartbeat_age_seconds === null ? "no heartbeat" : `${payload.worker.heartbeat_age_seconds}s ago`;
            $("#statJobDetail").textContent = `Worker ${payload.worker.status} · ${payload.worker.state} · ${workerAge}`;
            renderServiceHealth(payload.services);

            const statuses = Object.entries(payload.jobs.statuses).sort(([left], [right]) => left.localeCompare(right));
            $("#jobStatusList").innerHTML = statuses.length
                ? statuses.map(([status, count]) => `<span class="status-badge is-${status.replace(/[^a-z-]/g, "")}">${escapeHtml(status)}: ${Number(count)}</span>`).join("")
                : '<span class="quiet-badge">No jobs yet</span>';
            $("#statsUpdated").textContent = `Updated ${new Intl.DateTimeFormat(undefined, { timeStyle: "medium", hour12: false }).format(new Date())}`;
        } catch (error) {
            $("#statsUpdated").textContent = error.message;
            $("#serviceHealthList").innerHTML = `<span class="quiet-badge">${escapeHtml(error.message)}</span>`;
        } finally {
            button.disabled = false;
            refreshing = false;
            if (scheduleNext) scheduleRefresh();
        }
    }

    $("#refreshStats").addEventListener("click", () => refresh(false));
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            window.clearTimeout(refreshTimer);
            refresh(true);
        }
    });
    refresh(true);
})();
