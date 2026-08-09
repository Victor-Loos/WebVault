(() => {
    "use strict";

    const state = { archives: [], libraryCollections: [], jobs: [], collections: [], profiles: [] };
    const requestVersion = { archives: 0, jobs: 0, collections: 0 };
    const tabNames = ["capture", "library", "settings"];
    let seedHistoryTimer = null;
    let seedHistoryVersion = 0;
    let librarySearchTimer = null;
    let librarySearchVersion = 0;
    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

    const elements = {
        form: $("#crawlForm"),
        status: $("#crawlStatus"),
        jobList: $("#jobList"),
        archiveList: $("#archiveList"),
        managementList: $("#collectionListPanel"),
        dialog: $("#actionDialog"),
        dialogForm: $("#actionDialogForm"),
        dialogInputGroup: $("#dialogInputGroup"),
        dialogInput: $("#dialogInput"),
        dialogConfirm: $("#dialogConfirm"),
    };

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }

    function captureDate(value) {
        if (!value) return "";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return "";
        return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
    }

    function formatBytes(value) {
        const amount = Number(value || 0);
        if (!amount) return "0 B";
        const units = ["B", "KB", "MB", "GB", "TB"];
        const index = Math.min(Math.floor(Math.log(amount) / Math.log(1024)), units.length - 1);
        return `${(amount / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    function collectionLabel(value) {
        return String(value || "Not defined")
            .replaceAll("-", " ")
            .replaceAll("_", " ")
            .replace(/\b\w/g, (character) => character.toUpperCase());
    }

    async function api(path, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
            options.headers = { ...(options.headers || {}), "X-CSRF-Token": csrfToken };
        }
        const response = await fetch(path, options);
        const contentType = response.headers.get("content-type") || "";
        const payload = contentType.includes("json") ? await response.json() : { detail: await response.text() };
        if (!response.ok) {
            const validation = Array.isArray(payload.detail)
                ? payload.detail.map((item) => item.msg).join(". ")
                : payload.detail;
            throw new Error(payload.error || validation || `Request failed (${response.status})`);
        }
        return payload;
    }

    function toast(message, type = "success") {
        const item = document.createElement("div");
        item.className = `toast${type === "error" ? " is-error" : ""}`;
        item.textContent = message;
        $("#toastRegion").append(item);
        window.setTimeout(() => item.remove(), 4200);
    }

    function setStatus(message = "", type = "success") {
        elements.status.textContent = message;
        elements.status.classList.toggle("is-error", type === "error");
        elements.status.hidden = !message;
    }

    function slugify(value) {
        return value.toLowerCase().replace(/https?:\/\//g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").replace(/-{2,}/g, "-");
    }

    function suggestedCollection(rawSeed) {
        const trimmed = rawSeed.trim();
        if (!trimmed) return "";
        try {
            const url = new URL(trimmed.includes("://") ? trimmed : `https://${trimmed}`);
            const path = url.pathname.split("/").filter(Boolean).slice(0, 2);
            return slugify([url.hostname.replace(/^www\./, ""), ...path].join("-"));
        } catch {
            return "";
        }
    }

    function showTab(name, updateHash = true) {
        const target = tabNames.includes(name) ? name : "capture";
        $$('[data-tab-panel]').forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== target; });
        const captureIntro = $("#captureIntro");
        if (captureIntro) captureIntro.hidden = target !== "capture";
        $$('[data-tab-target]').forEach((button) => {
            const active = button.dataset.tabTarget === target;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-selected", String(active));
        });
        if (updateHash) history.replaceState(null, "", `#${target}`);
        if (target === "library") loadArchives();
        if (target === "settings") loadCollections();
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function renderCollectionSuggestions() {
        const datalist = $("#collectionSuggestions");
        datalist.replaceChildren(...state.collections.map((collection) => {
            const option = document.createElement("option");
            option.value = collection.name;
            option.label = collection.title || collectionLabel(collection.name);
            return option;
        }));
    }

    function renderArchives() {
        const query = $("#librarySearch").value.trim().toLowerCase();
        const field = $("#librarySearchField").value;
        const view = $("#libraryView").value;
        const sort = $("#librarySort").value;
        const source = view === "collections" ? state.libraryCollections : state.archives;
        elements.archiveList.hidden = field === "content" && query.length >= 2;
        const searchable = (item) => {
            if (!query || field === "content") return true;
            const values = {
                collection: item.name || item.collection || "",
                crawl: item.seed_url || "",
                title: item.title || "",
                date: `${item.latest_capture || ""} ${captureDate(item.latest_capture)}`,
                all: `${item.name || ""} ${item.collection || ""} ${item.seed_url || ""} ${item.title || ""} ${item.latest_capture || ""} ${captureDate(item.latest_capture)}`,
            };
            return String(values[field] ?? values.all).toLowerCase().includes(query);
        };
        const visible = source.filter(searchable).sort((left, right) => {
            if (sort.startsWith("alpha")) {
                const result = String(left.title || left.name || left.seed_url || "").localeCompare(String(right.title || right.name || right.seed_url || ""));
                return sort === "alpha-desc" ? -result : result;
            }
            const result = String(left.latest_capture || "").localeCompare(String(right.latest_capture || ""));
            return sort === "date-desc" ? -result : result;
        });
        if (!visible.length) {
            const hasArchives = source.length > 0;
            elements.archiveList.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><span class="empty-mark">${hasArchives ? "⌕" : "◇"}</span><strong>${hasArchives ? "No matching items" : "Your library is ready"}</strong><p>${hasArchives ? "Try another filter or search phrase." : "Complete your first capture and it will appear here."}</p>${hasArchives ? "" : '<button class="button button-primary" type="button" data-action="go-capture">Begin your first capture</button>'}</div>`;
            return;
        }
        elements.archiveList.innerHTML = visible.map((item, index) => view === "collections" ? `
            <article class="collection-card">
                <span class="collection-card-index">${String(index + 1).padStart(2, "0")}</span>
                <p class="eyebrow">${Number(item.file_count || 0)} capture${item.file_count === 1 ? "" : "s"}</p>
                <h3>${escapeHtml(item.title || collectionLabel(item.name))}</h3>
                <p class="collection-card-source">${escapeHtml(item.name)}</p>
                <p class="collection-card-meta">${formatBytes(item.size_bytes)}${captureDate(item.latest_capture) ? ` · Last saved ${escapeHtml(captureDate(item.latest_capture))}` : ""}</p>
                <div class="collection-card-actions"><a class="button button-primary" href="/archive/${encodeURIComponent(item.name)}">Open collection</a></div>
            </article>` : `
            <article class="collection-card">
                <span class="collection-card-index">${String(index + 1).padStart(2, "0")}</span>
                <p class="eyebrow">${escapeHtml(collectionLabel(item.collection))} · ${Number(item.version_count || 1)} version${item.version_count === 1 ? "" : "s"}</p>
                <h3>${escapeHtml(item.title || item.seed_url)}</h3>
                <p class="collection-card-source">${escapeHtml(item.seed_url || "")}</p>
                <p class="collection-card-meta">${Number(item.pages_count || 0)} pages · ${formatBytes(item.size_bytes)}${captureDate(item.latest_capture) ? ` · ${escapeHtml(captureDate(item.latest_capture))}` : ""}</p>
                <div class="collection-card-actions">
                    <a class="button button-primary" href="${item.job_id ? `/captures/${encodeURIComponent(item.job_id)}` : `/archive/${encodeURIComponent(item.collection)}`}">${item.job_id ? "Capture details" : "Open collection"}</a>
                    ${item.archive_url ? `<a class="button button-secondary" href="${escapeHtml(item.archive_url)}">Replay latest</a>` : ""}
                </div>
            </article>`).join("");
    }

    function jobStatusClass(status) {
        if (["failed", "cancelled"].includes(status)) return "is-failed";
        if (status === "completed") return "is-completed";
        if (["running", "queued", "uploading"].includes(status)) return "is-active";
        return "";
    }

    function renderJobs() {
        if (!state.jobs.length) {
            elements.jobList.classList.remove("is-scrollable");
            elements.jobList.innerHTML = '<div class="empty-state compact-empty"><span class="empty-mark" aria-hidden="true">◎</span><strong>No captures yet</strong><p>Your latest crawl progress will appear here.</p></div>';
            return;
        }
        const recentJobs = state.jobs.slice(0, 8);
        elements.jobList.classList.toggle("is-scrollable", recentJobs.length > 4);
        elements.jobList.innerHTML = recentJobs.map((job) => {
            const crawled = Number(job.pages_crawled || 0);
            const total = Number(job.pages_total || 0);
            const isActive = ["queued", "running", "uploading"].includes(job.status);
            const percent = total ? Math.min(100, Math.round((crawled / total) * 100)) : 0;
            const progress = isActive ? `<div class="job-progress-view">
                <div class="job-progress-summary"><strong>${job.status === "uploading" ? "Finalizing archive" : job.status === "queued" ? "Waiting for worker" : "Capturing website"}</strong><span>${total ? `${percent}%` : `${crawled} page${crawled === 1 ? "" : "s"}`}</span></div>
                <progress value="${total ? crawled : 0}" max="${total || 1}" aria-label="${total ? `${crawled} of ${total} pages` : `${crawled} pages captured`}"></progress>
                <div class="job-progress-metrics"><span><strong>${crawled}</strong> captured</span><span><strong>${Number(job.pages_pending || 0)}</strong> pending</span><span><strong>${Number(job.pages_failed || 0)}</strong> failed</span></div>
            </div>` : "";
            const live = job.status === "running" && job.live_url
                ? `<a class="button button-secondary" href="/live/?job=${encodeURIComponent(job.job_id)}">Watch live</a>` : "";
            const interact = job.status === "running" && job.interactive
                ? '<a class="button button-secondary" href="/live/control" target="_blank" rel="noreferrer">Interact</a>' : "";
            const replay = job.archive_url
                ? `<a class="button button-primary" href="${escapeHtml(job.archive_url)}">Replay</a>` : "";
            const rerun = job.crawl_request
                ? `<button class="button button-quiet" type="button" data-job-action="rerun" data-id="${escapeHtml(job.job_id)}">Run again</button>` : "";
            const cancel = isActive
                ? `<button class="button button-danger-quiet" type="button" data-job-action="cancel" data-id="${escapeHtml(job.job_id)}">Cancel</button>` : "";
            const information = job.job_type !== "profile"
                ? `<a class="button button-quiet" href="/captures/${encodeURIComponent(job.job_id)}">Capture info</a>` : "";
            const logs = job.recent_log?.length
                ? `<details class="job-details"><summary>Technical details</summary><pre>${escapeHtml(job.recent_log.join("\n"))}</pre></details>` : "";
            return `<article class="job-card ${isActive ? "is-active" : ""}">
                <div class="job-card-head"><span class="status-badge ${jobStatusClass(job.status)}">${escapeHtml(job.status || "unknown")}</span><span class="job-meta">${escapeHtml(job.started_at || "")}</span></div>
                <h3>${escapeHtml(collectionLabel(job.collection || job.job_id))}</h3>
                ${progress}
                <p class="job-message">${escapeHtml(job.message || "Waiting for an update…")}</p>
                ${job.current_url ? `<p class="job-current">${escapeHtml(job.current_url)}</p>` : ""}
                ${logs}
                <div class="job-actions">${information}${live}${interact}${replay}${rerun}${cancel}</div>
            </article>`;
        }).join("");
    }

    function renderManagement() {
        if (!state.collections.length) {
            elements.managementList.innerHTML = '<div class="empty-state"><strong>No collections to manage</strong><p>Your collections will appear here after the first capture.</p></div>';
            return;
        }
        elements.managementList.innerHTML = state.collections.map((collection) => `
            <article class="management-row">
                <div><strong>${escapeHtml(collection.title || collectionLabel(collection.name))}</strong><span>${collection.file_count} capture${collection.file_count === 1 ? "" : "s"} · ${escapeHtml(collection.name)}</span></div>
                <div class="management-actions">
                    ${collection.can_rerun ? `<button class="button button-quiet" type="button" data-manage-action="update" data-name="${escapeHtml(collection.name)}">Update</button>` : ""}
                    <button class="button button-quiet" type="button" data-manage-action="rename" data-name="${escapeHtml(collection.name)}">Rename</button>
                    <button class="button button-danger-quiet" type="button" data-manage-action="delete" data-name="${escapeHtml(collection.name)}">Delete</button>
                </div>
            </article>
        `).join("");
    }

    async function searchArchivedPages() {
        const query = $("#librarySearch").value.trim();
        const results = $("#searchResults");
        if ($("#librarySearchField").value !== "content" || query.length < 2) {
            results.hidden = true;
            results.replaceChildren();
            return;
        }
        const version = ++librarySearchVersion;
        try {
            const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
            if (version !== librarySearchVersion) return;
            results.hidden = false;
            results.innerHTML = data.results.length ? data.results.map((result) => {
                const snippet = escapeHtml(result.snippet || "")
                    .replaceAll("&lt;mark&gt;", "<mark>").replaceAll("&lt;/mark&gt;", "</mark>");
                return `<article class="management-row"><div><strong>${escapeHtml(result.title || result.url || "Archived page")}</strong><span>${escapeHtml(result.collection)} · ${escapeHtml(result.url)}</span><p>${snippet}</p></div>${result.archive_url ? `<a class="button button-secondary" href="${escapeHtml(result.archive_url)}">Replay capture</a>` : ""}</article>`;
            }).join("") : '<div class="empty-state"><strong>No matching page text</strong><p>Try fewer or broader words.</p></div>';
        } catch (error) {
            results.hidden = false;
            results.innerHTML = `<div class="empty-state"><strong>Full-text search unavailable</strong><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    function refreshLibrarySearch() {
        renderArchives();
        window.clearTimeout(librarySearchTimer);
        librarySearchTimer = window.setTimeout(searchArchivedPages, 300);
    }

    async function loadArchives() {
        const version = ++requestVersion.archives;
        try {
            const data = await api("/api/library");
            if (version !== requestVersion.archives) return;
            state.archives = data.sites || [];
            state.libraryCollections = data.collections || [];
            const captureCount = state.archives.reduce((total, site) => total + Number(site.version_count || 0), 0);
            $("#statCollections").textContent = String(state.libraryCollections.length);
            $("#statFiles").textContent = String(captureCount);
            $("#navLibraryCount").textContent = String(state.libraryCollections.length);
            renderArchives();
        } catch (error) {
            elements.archiveList.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><strong>Library unavailable</strong><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    async function loadJobs() {
        const version = ++requestVersion.jobs;
        try {
            const data = await api("/api/jobs");
            if (version !== requestVersion.jobs) return;
            state.jobs = data.jobs || [];
            $("#statJobs").textContent = String(state.jobs.length);
            renderJobs();
        } catch (error) {
            elements.jobList.innerHTML = `<div class="empty-state compact-empty"><strong>Activity unavailable</strong><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    async function loadProfiles() {
        try {
            const data = await api("/api/profiles");
            state.profiles = data.profiles || [];
            const select = $("#profileId");
            const selected = select.value || new URLSearchParams(location.search).get("profile") || "";
            select.innerHTML = '<option value="">No saved login</option>' + state.profiles.map((profile) => `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.name)}</option>`).join("");
            if (state.profiles.some((profile) => profile.id === selected)) select.value = selected;
        } catch (_error) {
            // Login profiles are optional; captures without one remain available.
        }
    }

    async function loadCollections() {
        const version = ++requestVersion.collections;
        try {
            const data = await api("/api/collections");
            if (version !== requestVersion.collections) return;
            state.collections = data.collections || [];
            renderCollectionSuggestions();
            renderManagement();
        } catch (error) {
            elements.managementList.innerHTML = `<div class="empty-state"><strong>Collections unavailable</strong><p>${escapeHtml(error.message)}</p></div>`;
        }
    }

    function closeDialog() {
        if (elements.dialog.open) elements.dialog.close();
    }

    function openDialog({ eyebrow = "Confirm action", title, description, confirm = "Continue", danger = false, input = null, onConfirm }) {
        $("#dialogEyebrow").textContent = eyebrow;
        $("#dialogTitle").textContent = title;
        $("#dialogDescription").textContent = description;
        elements.dialogConfirm.textContent = confirm;
        elements.dialogConfirm.className = `button ${danger ? "button-danger-quiet" : "button-primary"}`;
        elements.dialogInputGroup.hidden = !input;
        elements.dialogInput.required = Boolean(input);
        elements.dialogInput.value = input?.value || "";
        $("#dialogInputLabel").textContent = input?.label || "Value";
        elements.dialogForm.onsubmit = async (event) => {
            event.preventDefault();
            elements.dialogConfirm.disabled = true;
            try {
                await onConfirm(input ? elements.dialogInput.value.trim() : undefined);
                closeDialog();
            } catch (error) {
                toast(error.message, "error");
            } finally {
                elements.dialogConfirm.disabled = false;
            }
        };
        elements.dialog.showModal();
        window.setTimeout(() => (input ? elements.dialogInput : elements.dialogConfirm).focus(), 0);
    }

    function queueCollection(name, replace = false) {
        const action = replace ? "replace" : "update";
        openDialog({
            eyebrow: replace ? "Fresh replacement" : "Collection update",
            title: `${replace ? "Replace" : "Update"} ${collectionLabel(name)}?`,
            description: replace
                ? "A new crawl will run first. Existing captures are removed only after it succeeds."
                : "WebVault will reuse the latest crawl settings and add a new capture.",
            confirm: replace ? "Start replacement" : "Start update",
            onConfirm: async () => {
                await api(`/api/collections/${encodeURIComponent(name)}/crawl?replace=${replace}`, { method: "POST" });
                toast(`${collectionLabel(name)} was queued.`);
                showTab("capture");
                await loadJobs();
            },
        });
    }

    function createCollection() {
        openDialog({
            eyebrow: "New collection",
            title: "Create an empty collection",
            description: "Use collections to organize related captures. You can select it immediately from the capture form.",
            confirm: "Create collection",
            input: { label: "Collection name", value: "" },
            onConfirm: async (value) => {
                if (!value) throw new Error("Enter a collection name.");
                const result = await api("/api/collections", {
                    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: value }),
                });
                toast(`${collectionLabel(result.name)} was created.`);
                await Promise.all([loadCollections(), loadArchives()]);
            },
        });
    }

    function renameCollection(name) {
        openDialog({
            eyebrow: "Rename collection",
            title: "Give this collection a clearer name",
            description: "Its captures and metadata move together. Replay links will use the new collection address.",
            confirm: "Rename collection",
            input: { label: "Collection name", value: name },
            onConfirm: async (value) => {
                if (!value) throw new Error("Enter a collection name.");
                const result = await api(`/api/collections/${encodeURIComponent(name)}`, {
                    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: value }),
                });
                toast(`Renamed to ${collectionLabel(result.name)}.`);
                await Promise.all([loadCollections(), loadArchives()]);
            },
        });
    }

    function deleteCollection(name) {
        openDialog({
            eyebrow: "Permanent deletion", title: `Delete ${collectionLabel(name)}?`,
            description: "Every WACZ capture in this collection will be permanently removed. This cannot be undone.",
            confirm: "Delete permanently", danger: true,
            onConfirm: async () => {
                await api(`/api/collections/${encodeURIComponent(name)}`, { method: "DELETE" });
                toast(`${collectionLabel(name)} was deleted.`);
                await Promise.all([loadCollections(), loadArchives()]);
            },
        });
    }

    elements.form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const seeds = $("#seeds").value.split("\n").map((seed) => seed.trim()).filter(Boolean);
        if (!seeds.length) {
            setStatus("Add at least one website address before beginning.", "error");
            $("#seeds").focus();
            return;
        }
        const submit = $("#crawlSubmit");
        const original = submit.innerHTML;
        submit.disabled = true;
        submit.textContent = "Preparing capture…";
        setStatus("Checking the crawler and preparing your capture…");
        const payload = {
            seeds,
            collection: $("#collection").value.trim() || null,
            workers: Number.parseInt($("#workers").value, 10),
            depth: Number.parseInt($("#depth").value, 10),
            waitUntil: $("#waitUntil").value,
            profileId: $("#profileId").value || null,
            interactive: $("#interactive").checked,
            scopeType: $("#scopeType").value,
            pageLimit: Number.parseInt($("#pageLimit").value, 10) || 0,
            timeLimit: (Number.parseInt($("#timeLimit").value, 10) || 0) * 60,
            behaviors: ["autoscroll", "autoplay", "autofetch", "siteSpecific", ...($("#autoclick").checked ? ["autoclick"] : [])],
            screenshot: $("#screenshots").checked ? "view" : null,
            include: $("#includeRules").value.split("\n").map((rule) => rule.trim()).filter(Boolean),
            exclude: $("#excludeRules").value.split("\n").map((rule) => rule.trim()).filter(Boolean),
            scheduleInterval: $("#scheduleInterval").value,
            retentionCount: Number.parseInt($("#retentionCount").value, 10) || 0,
            generateWACZ: true, generateCDX: true, combineWARC: true,
        };
        try {
            const result = await api("/api/crawl", {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
            });
            setStatus(`Capture queued in ${collectionLabel(result.collection)}. Progress is now visible in Activity.`);
            elements.form.reset();
            $("#seedCount").textContent = "0 addresses";
            $("#previousCaptureNote").hidden = true;
            $("#collectionHint").textContent = "Leave blank to place this capture in “Not defined”.";
            await loadJobs();
        } catch (error) {
            setStatus(error.message, "error");
        } finally {
            submit.disabled = false;
            submit.innerHTML = original;
        }
    });

    $("#seeds").addEventListener("input", (event) => {
        const seeds = event.target.value.split("\n").map((seed) => seed.trim()).filter(Boolean);
        $("#seedCount").textContent = `${seeds.length} address${seeds.length === 1 ? "" : "es"}`;
        const suggestion = suggestedCollection(seeds[0] || "");
        $("#collectionHint").textContent = suggestion
            ? `Suggested collection: ${suggestion}`
            : "Leave blank to place this capture in “Not defined”.";
        const note = $("#previousCaptureNote");
        note.hidden = true;
        window.clearTimeout(seedHistoryTimer);
        const version = ++seedHistoryVersion;
        if (seeds[0]) {
            seedHistoryTimer = window.setTimeout(async () => {
                try {
                    const result = await api(`/api/captures/history?url=${encodeURIComponent(seeds[0])}`);
                    if (version !== seedHistoryVersion || !result.count) return;
                    const latest = result.captures[0];
                    note.innerHTML = `Previously captured ${result.count} time${result.count === 1 ? "" : "s"}. <a href="/captures/${encodeURIComponent(latest.job_id)}">View the latest record</a>. A new capture will be added to its history.`;
                    note.hidden = false;
                } catch (_error) {
                    // URL validation on submit remains authoritative.
                }
            }, 450);
        }
    });

    $$('[data-tab-target]').forEach((button, index, tabs) => {
        button.addEventListener("click", () => showTab(button.dataset.tabTarget));
        button.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
            tabs[nextIndex].focus();
            showTab(tabs[nextIndex].dataset.tabTarget);
        });
    });
    $$('[data-dialog-close]').forEach((button) => button.addEventListener("click", closeDialog));
    elements.dialog.addEventListener("click", (event) => { if (event.target === elements.dialog) closeDialog(); });
    $("#refreshJobs").addEventListener("click", loadJobs);
    $("#refreshArchives").addEventListener("click", loadArchives);
    $("#librarySearch").addEventListener("input", refreshLibrarySearch);
    function setLibraryControl(controlId, value) {
        $(`#${controlId}`).value = value;
        $$(`[data-control="${controlId}"] .choice-pill`).forEach((button) => {
            const selected = button.dataset.value === value;
            button.classList.toggle("is-selected", selected);
            button.setAttribute("aria-pressed", String(selected));
        });
    }
    $$("[data-control] .choice-pill").forEach((button) => {
        button.addEventListener("click", () => {
            const controlId = button.closest("[data-control]").dataset.control;
            setLibraryControl(controlId, button.dataset.value);
            if (controlId === "librarySearchField") {
                if (["crawl", "content"].includes(button.dataset.value)) setLibraryControl("libraryView", "crawls");
                if (button.dataset.value === "collection") setLibraryControl("libraryView", "collections");
            }
            if (controlId === "librarySort") renderArchives();
            else refreshLibrarySearch();
        });
    });

    elements.archiveList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-action]");
        if (!button) return;
        if (button.dataset.action === "go-capture") showTab("capture");
        if (button.dataset.action === "update") queueCollection(button.dataset.name, false);
        if (button.dataset.action === "replace") queueCollection(button.dataset.name, true);
    });
    $("#createCollection")?.addEventListener("click", createCollection);
    elements.managementList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-manage-action]");
        if (!button) return;
        const { manageAction: action, name } = button.dataset;
        if (action === "update") queueCollection(name, false);
        if (action === "rename") renameCollection(name);
        if (action === "delete") deleteCollection(name);
    });
    elements.jobList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-job-action]");
        if (!button) return;
        if (button.dataset.jobAction === "cancel") {
            openDialog({
                eyebrow: "Stop capture", title: "Cancel this capture?", danger: true,
                description: "Pages already processed may remain in temporary crawl output, but nothing new will be added to your archive.", confirm: "Cancel capture",
                onConfirm: async () => {
                    await api(`/api/jobs/${encodeURIComponent(button.dataset.id)}/cancel`, { method: "POST" });
                    toast("Cancellation requested."); await loadJobs();
                },
            });
            return;
        }
        openDialog({
            eyebrow: "Repeat capture", title: "Run this capture again?",
            description: "The original seeds and crawl settings will be reused in a new job.", confirm: "Run again",
            onConfirm: async () => {
                await api(`/api/jobs/${encodeURIComponent(button.dataset.id)}/rerun`, { method: "POST" });
                toast("Capture queued."); await loadJobs();
            },
        });
    });

    window.addEventListener("hashchange", () => {
        const hash = location.hash.slice(1);
        if (tabNames.includes(hash)) showTab(hash, false);
    });
    const initialTab = tabNames.includes(location.hash.slice(1)) ? location.hash.slice(1) : "capture";
    showTab(initialTab, false);
    const sourceJobId = new URLSearchParams(location.search).get("from_job");
    if (sourceJobId) {
        api(`/api/captures/${encodeURIComponent(sourceJobId)}`).then(async ({ capture }) => {
            const config = capture.crawl_request || {};
            $("#seeds").value = (config.seeds || []).join("\n");
            $("#collection").value = config.collection || "";
            for (const field of ["scopeType", "depth", "workers", "pageLimit", "waitUntil", "scheduleInterval", "retentionCount"]) {
                if (config[field] !== undefined) $(`#${field}`).value = String(config[field]);
            }
            if (config.timeLimit !== undefined) $("#timeLimit").value = String(Math.max(Math.round(config.timeLimit / 60), 1));
            $("#autoclick").checked = (config.behaviors || []).includes("autoclick");
            $("#screenshots").checked = Boolean(config.screenshot);
            $("#interactive").checked = Boolean(config.interactive);
            $("#includeRules").value = (config.include || []).join("\n");
            $("#excludeRules").value = (config.exclude || []).join("\n");
            if (config.profileId) {
                await loadProfiles();
                $("#profileId").value = config.profileId;
            }
            $("#seeds").dispatchEvent(new Event("input"));
            setStatus("Settings loaded from the selected capture. Review them before starting a new version.");
        }).catch((error) => setStatus(error.message, "error"));
    }
    Promise.all([loadArchives(), loadJobs(), loadCollections(), loadProfiles()]).then(() => {
        if (!location.hash && state.archives.length) showTab("library");
    });
    window.setInterval(() => { if (!document.hidden) loadJobs(); }, 5000);
    window.setInterval(() => { if (!document.hidden) loadArchives(); }, 15000);
})();
