(() => {
    "use strict";

    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
    const dialog = $("#fileDialog");
    const destination = $("#fileDestination");
    const moveButton = $("#moveFileButton");
    const deleteButton = $("#deleteFileButton");
    let currentCollection = "";
    let currentFilename = "";
    let deleteResetTimer = null;

    async function api(path, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
            options.headers = { ...(options.headers || {}), "X-CSRF-Token": csrfToken };
        }
        const response = await fetch(path, options);
        const contentType = response.headers.get("content-type") || "";
        const payload = contentType.includes("json") ? await response.json() : { detail: await response.text() };
        if (!response.ok) throw new Error(payload.error || payload.detail || `Request failed (${response.status})`);
        return payload;
    }

    function toast(message, type = "success") {
        const item = document.createElement("div");
        item.className = `toast${type === "error" ? " is-error" : ""}`;
        item.textContent = message;
        $("#toastRegion").append(item);
        window.setTimeout(() => item.remove(), 4200);
    }

    function resetDeleteButton() {
        window.clearTimeout(deleteResetTimer);
        deleteButton.dataset.confirming = "false";
        deleteButton.textContent = "Delete permanently";
        deleteButton.disabled = false;
    }

    function closeDialog() {
        resetDeleteButton();
        if (dialog.open) dialog.close();
    }

    async function loadCollectionSuggestions() {
        try {
            const data = await api("/api/collections");
            const options = (data.collections || []).map((collection) => {
                const option = document.createElement("option");
                option.value = collection.name;
                return option;
            });
            $("#fileCollectionSuggestions").replaceChildren(...options);
        } catch (error) {
            toast(error.message, "error");
        }
    }

    function openFileDialog(collection, filename) {
        currentCollection = collection;
        currentFilename = filename;
        $("#fileDialogName").textContent = `${collection}/${filename}`;
        destination.value = collection;
        resetDeleteButton();
        dialog.showModal();
        loadCollectionSuggestions();
        destination.focus();
    }

    $("#fileList").addEventListener("click", (event) => {
        const button = event.target.closest('[data-file-action="manage"]');
        if (button) openFileDialog(button.dataset.collection, button.dataset.filename);
    });

    $$('[data-dialog-close]').forEach((button) => button.addEventListener("click", closeDialog));
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(); });
    dialog.addEventListener("cancel", () => resetDeleteButton());

    moveButton.addEventListener("click", async () => {
        const target = destination.value.trim();
        if (!target) {
            toast("Choose a destination collection.", "error");
            destination.focus();
            return;
        }
        if (target === currentCollection) {
            toast("This capture is already in that collection.");
            closeDialog();
            return;
        }
        moveButton.disabled = true;
        moveButton.textContent = "Moving…";
        try {
            const result = await api(`/api/files/${encodeURIComponent(currentCollection)}/${encodeURIComponent(currentFilename)}`, {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ collection: target }),
            });
            toast(`Moved to ${result.to}.`);
            closeDialog();
            window.setTimeout(() => window.location.reload(), 500);
        } catch (error) {
            toast(error.message, "error");
        } finally {
            moveButton.disabled = false;
            moveButton.textContent = "Move capture";
        }
    });

    deleteButton.addEventListener("click", async () => {
        if (deleteButton.dataset.confirming !== "true") {
            deleteButton.dataset.confirming = "true";
            deleteButton.textContent = "Click again to delete";
            deleteResetTimer = window.setTimeout(resetDeleteButton, 5000);
            return;
        }
        deleteButton.disabled = true;
        deleteButton.textContent = "Deleting…";
        try {
            await api(`/api/files/${encodeURIComponent(currentCollection)}/${encodeURIComponent(currentFilename)}`, { method: "DELETE" });
            toast("Capture permanently deleted.");
            closeDialog();
            window.setTimeout(() => window.location.reload(), 500);
        } catch (error) {
            toast(error.message, "error");
            resetDeleteButton();
        }
    });

    const updateCollectionButton = $("#updateCollectionButton");
    if (updateCollectionButton) {
        updateCollectionButton.addEventListener("click", async () => {
            updateCollectionButton.disabled = true;
            updateCollectionButton.textContent = "Queueing…";
            try {
                await api(`/api/collections/${encodeURIComponent(updateCollectionButton.dataset.collection)}/crawl`, { method: "POST" });
                toast("A new version was queued.");
                window.setTimeout(() => { window.location.href = "/#capture"; }, 600);
            } catch (error) {
                toast(error.message, "error");
                updateCollectionButton.disabled = false;
                updateCollectionButton.textContent = "Capture new version";
            }
        });
    }

    const search = $("#fileSearch");
    if (search) {
        search.addEventListener("input", () => {
            const query = search.value.trim().toLowerCase();
            let visible = 0;
            $$("#fileList .collection-card").forEach((card) => {
                const matches = card.dataset.search.toLowerCase().includes(query);
                card.hidden = !matches;
                if (matches) visible += 1;
            });
            $("#fileSearchEmpty").hidden = visible > 0;
        });
    }
})();
