(function () {
  "use strict";

  const DB_NAME = "redVerdeWorkspace";
  const DB_VERSION = 1;
  const MODELS_STORE = "models";
  const VERSIONS_STORE = "versions";
  const CONTEXT_KEY = "redVerde_workspace_context_v1";
  const BOOTSTRAP_KEY = "redVerde_active_version_bootstrap_v1";
  const SNAPSHOT_SCHEMA_VERSION = 2;
  const EXPORT_FORMAT = "red-verde-workspace";
  const DEFAULT_PROJECT_NAME = "Proyecto_de_prueba_AALL";
  const LEGACY_KEYS = {
    activities: "redVerde_autocad_pdf_distancias_reales_v1",
    lineSettings: "redVerde_autocad_line_settings_v1",
    nodeSettings: "redVerde_autocad_node_settings_v1",
    filterSettings: "redVerde_autocad_filter_settings_v1",
    rockZones: "redVerde_autocad_rock_zones_v1",
    comments: "redVerde_autocad_comments_v1"
  };

  let database;
  let cloudProvider = null;
  let cloudSyncInProgress = false;
  let activeModel = null;
  let activeVersions = [];
  let context = readJson(CONTEXT_KEY, {});
  let feedbackTimer = null;

  const ui = buildInterface();
  wireInterface();
  initialize();

  function makeId(prefix) {
    const value = globalThis.crypto?.randomUUID?.() ||
      `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${value}`;
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function buildProjectCopyName(name, networkType = NETWORK_TYPE) {
    const type = String(networkType || NETWORK_TYPE).toUpperCase();
    const base = String(name || "Proyecto")
      .trim()
      .replace(/_(AALL|AASS|AAPP)$/i, "")
      .replace(/\s+/g, "_");
    return `${base}_copia_${type}`;
  }

  function readJson(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : clone(fallback);
    } catch {
      return clone(fallback);
    }
  }

  function writeJson(key, value) {
    localStorage.setItem(key, JSON.stringify(value));
  }

  function requestResult(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  function transactionDone(transaction) {
    return new Promise((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error("Transacción cancelada."));
    });
  }

  function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(MODELS_STORE)) {
          db.createObjectStore(MODELS_STORE, { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains(VERSIONS_STORE)) {
          const store = db.createObjectStore(VERSIONS_STORE, { keyPath: "id" });
          store.createIndex("modelId", "modelId", { unique: false });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function getAllModels() {
    const transaction = database.transaction(MODELS_STORE, "readonly");
    const models = await requestResult(transaction.objectStore(MODELS_STORE).getAll());
    return models.sort((a, b) =>
      String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
  }

  async function getModel(modelId) {
    const transaction = database.transaction(MODELS_STORE, "readonly");
    return requestResult(transaction.objectStore(MODELS_STORE).get(modelId));
  }

  async function getVersion(versionId) {
    const transaction = database.transaction(VERSIONS_STORE, "readonly");
    return requestResult(transaction.objectStore(VERSIONS_STORE).get(versionId));
  }

  async function getVersions(modelId) {
    const transaction = database.transaction(VERSIONS_STORE, "readonly");
    const index = transaction.objectStore(VERSIONS_STORE).index("modelId");
    const versions = await requestResult(index.getAll(IDBKeyRange.only(modelId)));
    return versions
      .filter(version => !version.deletedAt)
      .sort((a, b) =>
        String(b.savedAt || "").localeCompare(String(a.savedAt || "")) ||
        Number(b.versionNumber) - Number(a.versionNumber));
  }

  async function putModel(model) {
    const transaction = database.transaction(MODELS_STORE, "readwrite");
    transaction.objectStore(MODELS_STORE).put(model);
    await transactionDone(transaction);
  }

  function captureSnapshot() {
    const network = {
      type: NETWORK_TYPE,
      definition: clone(NETWORK_DEFINITION),
      nodes: clone(nodeDefs),
      lines: clone(lineDefs),
      routeEdges: clone(routeEdges),
      viewBox: clone(INITIAL_VIEW_BOX)
    };

    return {
      schemaVersion: SNAPSHOT_SCHEMA_VERSION,
      network,
      state: {
        activities: readJson(LEGACY_KEYS.activities, []),
        lineSettings: readJson(LEGACY_KEYS.lineSettings, {}),
        nodeSettings: readJson(LEGACY_KEYS.nodeSettings, {}),
        filterSettings: readJson(LEGACY_KEYS.filterSettings, {}),
        rockZones: readJson(LEGACY_KEYS.rockZones, []),
        comments: readJson(LEGACY_KEYS.comments, [])
      }
    };
  }

  function applySnapshot(snapshot) {
    if (!snapshot?.network || !snapshot?.state) {
      throw new Error("La versión no contiene un estado válido.");
    }

    writeJson(BOOTSTRAP_KEY, {
      schemaVersion: snapshot.schemaVersion || SNAPSHOT_SCHEMA_VERSION,
      network: snapshot.network
    });
    writeJson(LEGACY_KEYS.activities, snapshot.state.activities || []);
    writeJson(LEGACY_KEYS.lineSettings, snapshot.state.lineSettings || {});
    writeJson(LEGACY_KEYS.nodeSettings, snapshot.state.nodeSettings || {});
    writeJson(LEGACY_KEYS.filterSettings, snapshot.state.filterSettings || {});
    writeJson(LEGACY_KEYS.rockZones, snapshot.state.rockZones || []);
    writeJson(LEGACY_KEYS.comments, snapshot.state.comments || []);
  }

  async function createModel(name, snapshot, note = "Versión inicial") {
    const now = new Date().toISOString();
    const model = {
      id: makeId("model"),
      name: name.trim(),
      networkType: snapshot?.network?.type || NETWORK_TYPE,
      createdAt: now,
      updatedAt: now,
      currentVersionId: null,
      nextVersionNumber: 1
    };
    const version = {
      id: makeId("version"),
      modelId: model.id,
      versionNumber: 1,
      savedAt: now,
      note,
      parentVersionId: null,
      schemaVersion: SNAPSHOT_SCHEMA_VERSION,
      snapshot: clone(snapshot),
      syncState: "local"
    };
    model.currentVersionId = version.id;
    model.nextVersionNumber = 2;

    const transaction = database.transaction([MODELS_STORE, VERSIONS_STORE], "readwrite");
    transaction.objectStore(MODELS_STORE).add(model);
    transaction.objectStore(VERSIONS_STORE).add(version);
    await transactionDone(transaction);
    return { model, version };
  }

  async function createVersion(modelId, snapshot, metadata = {}) {
    const transaction = database.transaction([MODELS_STORE, VERSIONS_STORE], "readwrite");
    const modelsStore = transaction.objectStore(MODELS_STORE);
    const versionsStore = transaction.objectStore(VERSIONS_STORE);
    const model = await requestResult(modelsStore.get(modelId));
    if (!model) {
      transaction.abort();
      throw new Error("No se encontró el proyecto activo.");
    }

    const now = new Date().toISOString();
    const version = {
      id: makeId("version"),
      modelId,
      versionNumber: Number(model.nextVersionNumber) || 1,
      savedAt: now,
      note: String(metadata.note || "").trim(),
      parentVersionId: metadata.parentVersionId || context.loadedVersionId || model.currentVersionId || null,
      schemaVersion: SNAPSHOT_SCHEMA_VERSION,
      snapshot: clone(snapshot),
      syncState: "local"
    };

    model.currentVersionId = version.id;
    model.nextVersionNumber = version.versionNumber + 1;
    model.updatedAt = now;
    versionsStore.add(version);
    modelsStore.put(model);
    await transactionDone(transaction);
    return { model, version };
  }

  function getBuiltInProjects() {
    return Array.isArray(globalThis.RED_NETWORK_BUILTIN_PROJECTS)
      ? globalThis.RED_NETWORK_BUILTIN_PROJECTS
      : [];
  }

  async function seedBuiltInProjects(models) {
    const created = [];
    for (const project of getBuiltInProjects()) {
      const snapshot = clone(project.snapshot);
      if (!snapshot.network.definition) {
        snapshot.network.definition = clone(NETWORK_DEFINITION);
      }
      const revision = Number(project.revision) || 1;
      const existingBuiltIn = models.find(model => model.builtInKey === project.key);
      if (existingBuiltIn) {
        if ((Number(existingBuiltIn.builtInRevision) || 1) >= revision) continue;
        const result = await createVersion(existingBuiltIn.id, snapshot, {
          note: `${project.note || "Modelo incorporado"} · revisión ${revision}`,
          parentVersionId: existingBuiltIn.currentVersionId
        });
        result.model.builtInKey = project.key;
        result.model.builtInRevision = revision;
        result.model.networkType = project.networkType || snapshot.network.type || NETWORK_TYPE;
        result.model.sourceMetadata = clone(project.metadata || {});
        await putModel(result.model);
        models.splice(models.indexOf(existingBuiltIn), 1, result.model);
        created.push(result);
        continue;
      }

      const existingByName = models.find(model => model.name === project.name);
      if (existingByName) continue;

      const result = await createModel(project.name, snapshot, project.note || "Modelo incorporado");
      result.model.builtInKey = project.key;
      result.model.builtInRevision = revision;
      result.model.networkType = project.networkType || snapshot.network.type || NETWORK_TYPE;
      result.model.sourceMetadata = clone(project.metadata || {});
      await putModel(result.model);
      models.push(result.model);
      created.push(result);
    }
    return created;
  }

  async function initialize() {
    try {
      database = await openDatabase();
      let models = await getAllModels();

      if (!models.length) {
        const initial = await createModel(DEFAULT_PROJECT_NAME, captureSnapshot(),
          "Migración inicial del proyecto existente");
        models = [initial.model];
        context = {
          activeModelId: initial.model.id,
          loadedVersionId: initial.version.id
        };
        writeJson(CONTEXT_KEY, context);
      }

      for (const model of models) {
        if (model.name === "Modelo de prueba") {
          model.name = `Proyecto_de_prueba_${model.networkType || NETWORK_TYPE}`;
          model.networkType = model.networkType || NETWORK_TYPE;
          model.updatedAt = new Date().toISOString();
          await putModel(model);
        }
      }

      const createdBuiltIns = await seedBuiltInProjects(models);
      if (createdBuiltIns.length) {
        const preferred = createdBuiltIns.find(result =>
          result.model.name === "Costanera_acacias_AALL") || createdBuiltIns[0];
        context = {
          activeModelId: preferred.model.id,
          loadedVersionId: preferred.version.id
        };
        writeJson(CONTEXT_KEY, context);
        applySnapshot(preferred.version.snapshot);
        location.reload();
        return;
      }

      models = await getAllModels();

      activeModel = models.find(model => model.id === context.activeModelId) || models[0];
      activeVersions = await getVersions(activeModel.id);
      let loadedVersion = context.loadedVersionId
        ? await getVersion(context.loadedVersionId)
        : null;
      if (!loadedVersion || loadedVersion.deletedAt || loadedVersion.modelId !== activeModel.id) {
        loadedVersion = activeVersions.find(version =>
          version.id === activeModel.currentVersionId) || activeVersions[0] || null;
      }
      context = {
        activeModelId: activeModel.id,
        loadedVersionId: loadedVersion?.id || null
      };
      writeJson(CONTEXT_KEY, context);
      updateModelTitle();
      await render();
      setFeedback();
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo iniciar la biblioteca de versiones.", "error");
      ui.saveButton.disabled = true;
    }
  }

  function buildInterface() {
    const header = document.querySelector(".model-header");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "version-library-toggle";
    toggle.setAttribute("aria-label", "Abrir proyectos y versiones");
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "☰";
    header?.prepend(toggle);

    const backdrop = document.createElement("div");
    backdrop.className = "version-library-backdrop";

    const panel = document.createElement("aside");
    panel.className = "version-library";
    panel.setAttribute("aria-hidden", "true");
    panel.innerHTML = `
      <header class="version-library-header">
        <div>
          <p class="version-library-kicker">Biblioteca</p>
          <h2 class="version-library-title">Proyectos</h2>
        </div>
        <button class="version-library-close" type="button" aria-label="Cerrar">×</button>
      </header>
      <div class="version-library-content">
        <section class="version-card version-main-card">
          <div class="version-model-row">
            <div class="version-field">
              <label for="versionModelSelect">Proyecto</label>
              <select id="versionModelSelect"></select>
            </div>
            <div class="version-overflow">
              <button
                id="moreActionsButton"
                class="version-more-button"
                type="button"
                aria-label="Más opciones"
                aria-haspopup="menu"
                aria-expanded="false"
              >⋯</button>
              <div id="moreActionsMenu" class="version-overflow-menu" role="menu" hidden>
                <button id="renameModelButton" type="button" role="menuitem">Renombrar proyecto</button>
                <button id="duplicateModelButton" type="button" role="menuitem">Duplicar proyecto</button>
                <div class="version-menu-separator"></div>
                <button id="addNoteButton" type="button" role="menuitem">Añadir comentario</button>
                <button id="showHistoryButton" type="button" role="menuitem">Ver historial</button>
                <div class="version-menu-separator"></div>
                <button id="exportWorkspaceButton" type="button" role="menuitem">Exportar respaldo</button>
                <label class="version-menu-import" role="menuitem">
                  Importar respaldo
                  <input id="importWorkspaceInput" type="file" accept="application/json,.json" />
                </label>
              </div>
            </div>
          </div>
          <p class="version-current">Versión actual <strong id="loadedVersionLabel">—</strong></p>
          <div id="versionNotePanel" class="version-optional-panel" hidden>
            <div class="version-field">
              <label for="versionNote">Comentario</label>
              <textarea id="versionNote" maxlength="300" placeholder="¿Qué cambió en esta versión?"></textarea>
            </div>
          </div>
          <button id="saveNewVersionButton" class="version-primary-action" type="button">
            Guardar
          </button>
          <p id="versionFeedback" class="version-feedback" aria-live="polite"></p>
          <button id="cloudConnectionButton" class="version-sync-status" type="button" disabled>
            <span class="version-sync-dot"></span>
            <span>Solo en este dispositivo</span>
          </button>
        </section>

        <section id="versionHistoryPanel" class="version-card version-history-panel" hidden>
          <div class="version-section-heading">
            <h3 class="version-card-title">Historial</h3>
            <button id="closeHistoryButton" type="button" aria-label="Cerrar historial">×</button>
          </div>
          <div id="versionsList" class="version-list"></div>
        </section>
      </div>
    `;

    document.body.appendChild(backdrop);
    document.body.appendChild(panel);

    return {
      toggle,
      backdrop,
      panel,
      close: panel.querySelector(".version-library-close"),
      modelSelect: panel.querySelector("#versionModelSelect"),
      moreButton: panel.querySelector("#moreActionsButton"),
      moreMenu: panel.querySelector("#moreActionsMenu"),
      duplicateButton: panel.querySelector("#duplicateModelButton"),
      renameButton: panel.querySelector("#renameModelButton"),
      noteMenuButton: panel.querySelector("#addNoteButton"),
      notePanel: panel.querySelector("#versionNotePanel"),
      note: panel.querySelector("#versionNote"),
      historyMenuButton: panel.querySelector("#showHistoryButton"),
      historyPanel: panel.querySelector("#versionHistoryPanel"),
      closeHistoryButton: panel.querySelector("#closeHistoryButton"),
      saveButton: panel.querySelector("#saveNewVersionButton"),
      feedback: panel.querySelector("#versionFeedback"),
      cloudButton: panel.querySelector("#cloudConnectionButton"),
      exportButton: panel.querySelector("#exportWorkspaceButton"),
      importInput: panel.querySelector("#importWorkspaceInput"),
      loadedVersionLabel: panel.querySelector("#loadedVersionLabel"),
      versionsList: panel.querySelector("#versionsList")
    };
  }

  function wireInterface() {
    ui.toggle.addEventListener("click", () => setPanelOpen(true));
    ui.close.addEventListener("click", () => setPanelOpen(false));
    ui.backdrop.addEventListener("click", () => setPanelOpen(false));
    ui.saveButton.addEventListener("click", saveNewVersion);
    ui.moreButton.addEventListener("click", event => {
      event.stopPropagation();
      setOverflowOpen(ui.moreMenu.hidden);
    });
    ui.duplicateButton.addEventListener("click", () => runMenuAction(duplicateActiveModel));
    ui.renameButton.addEventListener("click", () => runMenuAction(renameActiveModel));
    ui.noteMenuButton.addEventListener("click", () => {
      setOverflowOpen(false);
      ui.notePanel.hidden = !ui.notePanel.hidden;
      ui.noteMenuButton.textContent = ui.notePanel.hidden
        ? "Añadir comentario"
        : "Ocultar comentario";
      if (!ui.notePanel.hidden) ui.note.focus();
    });
    ui.historyMenuButton.addEventListener("click", () => {
      setOverflowOpen(false);
      setHistoryOpen(ui.historyPanel.hidden);
    });
    ui.closeHistoryButton.addEventListener("click", () => setHistoryOpen(false));
    ui.exportButton.addEventListener("click", () => runMenuAction(exportWorkspace));
    ui.importInput.addEventListener("change", importWorkspace);
    ui.cloudButton.addEventListener("click", () => synchronizeCloud(false));
    ui.modelSelect.addEventListener("change", () => switchModel(ui.modelSelect.value));
    document.addEventListener("click", event => {
      if (!event.target.closest(".version-overflow")) setOverflowOpen(false);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && ui.panel.classList.contains("open")) {
        if (!ui.moreMenu.hidden) setOverflowOpen(false);
        else setPanelOpen(false);
      }
    });
  }

  function setPanelOpen(open) {
    if (!open) setOverflowOpen(false);
    ui.panel.classList.toggle("open", open);
    ui.backdrop.classList.toggle("open", open);
    ui.panel.setAttribute("aria-hidden", open ? "false" : "true");
    ui.toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function setOverflowOpen(open) {
    ui.moreMenu.hidden = !open;
    ui.moreButton.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function runMenuAction(action) {
    setOverflowOpen(false);
    return action();
  }

  function setHistoryOpen(open) {
    ui.historyPanel.hidden = !open;
    ui.historyMenuButton.textContent = open ? "Ocultar historial" : "Ver historial";
    if (open) ui.historyPanel.scrollIntoView({ block: "nearest" });
  }

  function today() {
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 10);
  }

  function formatDateTime(iso) {
    try {
      return new Intl.DateTimeFormat("es", {
        dateStyle: "short",
        timeStyle: "short"
      }).format(new Date(iso));
    } catch {
      return iso || "";
    }
  }

  function getDisplayVersionNumber(versionId) {
    const index = activeVersions.findIndex(version => version.id === versionId);
    return index >= 0 ? activeVersions.length - index : null;
  }

  function formatVersionLabel(versionId) {
    const number = getDisplayVersionNumber(versionId);
    return number ? `v${String(number).padStart(3, "0")}` : "—";
  }

  function setFeedback(message = "", type = "") {
    clearTimeout(feedbackTimer);
    ui.feedback.textContent = message;
    ui.feedback.className = `version-feedback${type ? ` ${type}` : ""}`;
    if (message && type) {
      feedbackTimer = setTimeout(() => setFeedback(), type === "error" ? 5000 : 3000);
    }
  }

  function setSyncStatus(message, type = "", actionable = false) {
    ui.cloudButton.className = `version-sync-status${type ? ` ${type}` : ""}${actionable ? " actionable" : ""}`;
    ui.cloudButton.lastElementChild.textContent = message;
    ui.cloudButton.disabled = !actionable;
  }

  function updateModelTitle() {
    const title = document.querySelector(".model-title");
    if (title && activeModel) title.textContent = activeModel.name;
  }

  async function render() {
    const models = await getAllModels();
    ui.modelSelect.innerHTML = "";
    models.forEach(model => {
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = model.name;
      option.selected = model.id === activeModel?.id;
      ui.modelSelect.appendChild(option);
    });

    ui.loadedVersionLabel.textContent = formatVersionLabel(context.loadedVersionId);
    ui.versionsList.innerHTML = "";

    if (!activeVersions.length) {
      ui.versionsList.innerHTML = '<p class="version-list-empty">Todavía no hay versiones.</p>';
      return;
    }

    activeVersions.forEach(version => {
      const item = document.createElement("article");
      const isLoaded = version.id === context.loadedVersionId;
      const versionLabel = formatVersionLabel(version.id);
      item.className = `version-item${isLoaded ? " active" : ""}`;
      const content = document.createElement("div");
      const title = document.createElement("p");
      const meta = document.createElement("p");
      title.className = "version-item-title";
      meta.className = "version-item-meta";
      title.textContent = versionLabel;
      meta.textContent = formatDateTime(version.savedAt);
      content.append(title, meta);
      if (version.note) {
        const note = document.createElement("p");
        note.className = "version-item-note";
        note.textContent = version.note;
        content.appendChild(note);
      }
      const actions = document.createElement("div");
      actions.className = "version-item-actions";
      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "version-open-button";
      openButton.textContent = isLoaded ? "Abierta" : "Abrir";
      openButton.disabled = isLoaded;
      openButton.addEventListener("click", () => openVersion(version.id));
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "version-delete-button";
      deleteButton.textContent = "×";
      deleteButton.title = activeVersions.length > 1
        ? `Eliminar ${versionLabel}`
        : "El proyecto debe conservar al menos una versión";
      deleteButton.setAttribute("aria-label", deleteButton.title);
      deleteButton.disabled = activeVersions.length <= 1;
      deleteButton.addEventListener("click", () => deleteVersion(version.id));
      actions.append(openButton, deleteButton);
      item.append(content, actions);
      ui.versionsList.appendChild(item);
    });
  }

  async function saveNewVersion() {
    if (!activeModel || ui.saveButton.disabled) return;
    ui.saveButton.disabled = true;
    ui.saveButton.textContent = "Guardando…";
    setFeedback("Guardando…");
    try {
      const result = await createVersion(activeModel.id, captureSnapshot(), {
        note: ui.note.value
      });
      activeModel = result.model;
      context.loadedVersionId = result.version.id;
      writeJson(CONTEXT_KEY, context);
      activeVersions = await getVersions(activeModel.id);
      ui.note.value = "";
      ui.notePanel.hidden = true;
      ui.noteMenuButton.textContent = "Añadir comentario";
      await render();
      setFeedback(
        `Guardado · ${formatVersionLabel(result.version.id)}`,
        "success"
      );
      if (cloudProvider?.isSignedIn?.()) {
        try {
          await cloudProvider.pushVersion(clone(activeModel), clone(result.version));
          setSyncStatus("En la nube", "ready");
        } catch (cloudError) {
          console.error(cloudError);
          setSyncStatus("Pendiente de subir · Reintentar", "error", true);
        }
      } else {
        setSyncStatus(
          cloudProvider ? "Conectar con Google" : "Solo en este dispositivo",
          "",
          Boolean(cloudProvider)
        );
      }
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo guardar la nueva versión.", "error");
    } finally {
      ui.saveButton.disabled = false;
      ui.saveButton.textContent = "Guardar";
    }
  }

  async function currentWorkDiffersFromLoaded() {
    if (!context.loadedVersionId) return false;
    const loaded = await getVersion(context.loadedVersionId);
    if (!loaded) return false;
    return JSON.stringify(captureSnapshot()) !== JSON.stringify(loaded.snapshot);
  }

  async function confirmDiscardingUnsavedWork() {
    const differs = await currentWorkDiffersFromLoaded();
    if (!differs) return true;
    return confirm(
      "Hay cambios posteriores a la última versión guardada. " +
      "Si continúas, esos cambios no quedarán en el historial. ¿Deseas continuar?"
    );
  }

  async function openVersion(versionId) {
    try {
      if (!await confirmDiscardingUnsavedWork()) return;
      const version = await getVersion(versionId);
      if (!version || version.deletedAt) throw new Error("Versión no encontrada.");
      const model = await getModel(version.modelId);
      applySnapshot(version.snapshot);
      context = {
        activeModelId: model.id,
        loadedVersionId: version.id
      };
      writeJson(CONTEXT_KEY, context);
      location.reload();
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo abrir la versión.", "error");
    }
  }

  async function markVersionAsDeleted(versionId) {
    const transaction = database.transaction([MODELS_STORE, VERSIONS_STORE], "readwrite");
    const modelsStore = transaction.objectStore(MODELS_STORE);
    const versionsStore = transaction.objectStore(VERSIONS_STORE);
    const version = await requestResult(versionsStore.get(versionId));
    if (!version || version.deletedAt) {
      transaction.abort();
      throw new Error("Versión no encontrada.");
    }
    const model = await requestResult(modelsStore.get(version.modelId));
    const allVersions = await requestResult(
      versionsStore.index("modelId").getAll(IDBKeyRange.only(version.modelId))
    );
    const remaining = allVersions
      .filter(item => item.id !== versionId && !item.deletedAt)
      .sort((a, b) =>
        String(b.savedAt || "").localeCompare(String(a.savedAt || "")) ||
        Number(b.versionNumber) - Number(a.versionNumber));
    if (!model || !remaining.length) {
      transaction.abort();
      throw new Error("El proyecto debe conservar al menos una versión.");
    }

    const now = new Date().toISOString();
    version.deletedAt = now;
    version.updatedAt = now;
    if (model.currentVersionId === versionId) {
      model.currentVersionId = remaining[0].id;
    }
    model.updatedAt = now;
    versionsStore.put(version);
    modelsStore.put(model);
    await transactionDone(transaction);
    return { model, version, replacement: remaining[0] };
  }

  async function deleteVersion(versionId) {
    if (activeVersions.length <= 1) {
      setFeedback("El proyecto debe conservar al menos una versión.", "error");
      return;
    }

    const version = activeVersions.find(item => item.id === versionId);
    if (!version) return;
    const versionLabel = formatVersionLabel(versionId);
    const isLoaded = versionId === context.loadedVersionId;
    const hasUnsavedWork = isLoaded && await currentWorkDiffersFromLoaded();
    const warning = [
      `¿Eliminar ${versionLabel}?`,
      "Las versiones posteriores cambiarán de número.",
      isLoaded ? "Se abrirá la versión más reciente disponible." : "",
      hasUnsavedWork ? "Los cambios que todavía no has guardado se perderán." : ""
    ].filter(Boolean).join("\n\n");
    if (!confirm(warning)) return;

    try {
      const result = await markVersionAsDeleted(versionId);
      activeModel = result.model;
      activeVersions = await getVersions(activeModel.id);

      if (cloudProvider?.isSignedIn?.()) {
        try {
          await cloudProvider.pushVersion(clone(activeModel), clone(result.version));
          setSyncStatus("En la nube", "ready");
        } catch (cloudError) {
          console.error(cloudError);
          setSyncStatus("Pendiente de subir · Reintentar", "error", true);
        }
      } else {
        setSyncStatus(
          cloudProvider ? "Conectar con Google" : "Solo en este dispositivo",
          "",
          Boolean(cloudProvider)
        );
      }

      if (isLoaded) {
        const replacement = activeVersions.find(item =>
          item.id === activeModel.currentVersionId) || activeVersions[0];
        applySnapshot(replacement.snapshot);
        context.loadedVersionId = replacement.id;
        writeJson(CONTEXT_KEY, context);
        location.reload();
        return;
      }

      await render();
      setFeedback(`${versionLabel} eliminada.`, "success");
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo eliminar la versión.", "error");
    }
  }

  async function switchModel(modelId) {
    if (!modelId || modelId === activeModel?.id) return;
    if (!await confirmDiscardingUnsavedWork()) {
      ui.modelSelect.value = activeModel.id;
      return;
    }
    const model = await getModel(modelId);
    let version = model ? await getVersion(model.currentVersionId) : null;
    if (version?.deletedAt) version = null;
    if (!version && model) {
      version = (await getVersions(model.id))[0] || null;
    }
    if (!model || !version) {
      setFeedback("El proyecto no tiene una versión válida.", "error");
      return;
    }
    applySnapshot(version.snapshot);
    writeJson(CONTEXT_KEY, {
      activeModelId: model.id,
      loadedVersionId: version.id
    });
    location.reload();
  }

  async function duplicateActiveModel() {
    const suggested = buildProjectCopyName(activeModel?.name, activeModel?.networkType);
    const name = prompt("Nombre del nuevo proyecto (ej.: Costanera_acacias_AALL):", suggested)?.trim();
    if (!name) return;
    try {
      const result = await createModel(name, captureSnapshot(), "Proyecto duplicado");
      applySnapshot(result.version.snapshot);
      writeJson(CONTEXT_KEY, {
        activeModelId: result.model.id,
        loadedVersionId: result.version.id
      });
      location.reload();
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo duplicar el proyecto.", "error");
    }
  }

  async function renameActiveModel() {
    const name = prompt("Nuevo nombre del proyecto (ej.: Oryza_AALL):", activeModel?.name || "")?.trim();
    if (!name || name === activeModel?.name) return;
    try {
      activeModel.name = name;
      activeModel.updatedAt = new Date().toISOString();
      await putModel(activeModel);
      updateModelTitle();
      await render();
      setFeedback("Proyecto renombrado.", "success");
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo renombrar el proyecto.", "error");
    }
  }

  async function getWorkspaceData() {
    const models = await getAllModels();
    const transaction = database.transaction(VERSIONS_STORE, "readonly");
    const versions = await requestResult(transaction.objectStore(VERSIONS_STORE).getAll());
    return {
      format: EXPORT_FORMAT,
      formatVersion: 1,
      exportedAt: new Date().toISOString(),
      context: clone(context),
      models: clone(models),
      versions: clone(versions)
    };
  }

  async function exportWorkspace() {
    try {
      const data = await getWorkspaceData();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `red-verde-copia-${today()}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setFeedback("Copia completa exportada.", "success");
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo exportar la copia.", "error");
    }
  }

  function validateWorkspace(data) {
    return data &&
      data.format === EXPORT_FORMAT &&
      Array.isArray(data.models) &&
      Array.isArray(data.versions) &&
      data.models.length > 0 &&
      data.versions.every(version => version?.id && version?.modelId && version?.snapshot);
  }

  async function replaceWorkspace(data) {
    const transaction = database.transaction([MODELS_STORE, VERSIONS_STORE], "readwrite");
    const modelsStore = transaction.objectStore(MODELS_STORE);
    const versionsStore = transaction.objectStore(VERSIONS_STORE);
    modelsStore.clear();
    versionsStore.clear();
    data.models.forEach(model => modelsStore.put(model));
    data.versions.forEach(version => versionsStore.put(version));
    await transactionDone(transaction);
  }

  async function importWorkspace(event) {
    setOverflowOpen(false);
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!validateWorkspace(data)) throw new Error("Formato de copia no válido.");
      const accepted = confirm(
        "La copia importada reemplazará la biblioteca local actual. " +
        "Conviene exportar primero la biblioteca actual. ¿Deseas continuar?"
      );
      if (!accepted) return;
      await replaceWorkspace(data);
      const importedContext = data.context || {};
      const model = data.models.find(item => item.id === importedContext.activeModelId) ||
        data.models[0];
      const version = data.versions.find(item =>
        !item.deletedAt &&
        item.id === importedContext.loadedVersionId &&
        item.modelId === model.id) ||
        data.versions
          .filter(item => item.modelId === model.id && !item.deletedAt)
          .sort((a, b) =>
            String(b.savedAt || "").localeCompare(String(a.savedAt || "")) ||
            Number(b.versionNumber) - Number(a.versionNumber))[0];
      if (!version) throw new Error("La copia no contiene una versión utilizable.");
      applySnapshot(version.snapshot);
      writeJson(CONTEXT_KEY, {
        activeModelId: model.id,
        loadedVersionId: version.id
      });
      location.reload();
    } catch (error) {
      console.error(error);
      setFeedback("El archivo no contiene una copia válida.", "error");
    }
  }

  async function mergeWorkspace(remote) {
    if (!remote || !Array.isArray(remote.models) || !Array.isArray(remote.versions)) return;
    const local = await getWorkspaceData();
    const localModelMap = new Map(local.models.map(model => [model.id, model]));
    const localVersionMap = new Map(local.versions.map(version => [version.id, version]));
    const transaction = database.transaction([MODELS_STORE, VERSIONS_STORE], "readwrite");
    const modelsStore = transaction.objectStore(MODELS_STORE);
    const versionsStore = transaction.objectStore(VERSIONS_STORE);

    remote.models.forEach(model => {
      const existing = localModelMap.get(model.id);
      if (!existing || String(model.updatedAt || "") > String(existing.updatedAt || "")) {
        modelsStore.put(model);
      }
    });
    remote.versions.forEach(version => {
      const existing = localVersionMap.get(version.id);
      if (!existing) {
        versionsStore.add(version);
        return;
      }
      if (
        version.deletedAt &&
        (!existing.deletedAt || String(version.deletedAt) > String(existing.deletedAt))
      ) {
        versionsStore.put(version);
      }
    });
    await transactionDone(transaction);
  }

  async function synchronizeCloud(silent = false) {
    if (cloudSyncInProgress) return;
    if (!cloudProvider) {
      setSyncStatus("Solo en este dispositivo");
      if (!silent) setFeedback("La nube todavía no está disponible.", "error");
      return;
    }
    cloudSyncInProgress = true;
    setSyncStatus("Actualizando…");
    try {
      if (!cloudProvider.isSignedIn()) await cloudProvider.signIn();
      const remote = await cloudProvider.pullWorkspace();
      await mergeWorkspace(remote);
      const merged = await getWorkspaceData();
      await cloudProvider.pushWorkspace(merged);
      setSyncStatus("En la nube", "ready");
      if (!silent) setFeedback("Actualizado en la nube.", "success");
      activeModel = await getModel(context.activeModelId);
      activeVersions = await getVersions(context.activeModelId);
      let latestVersion = activeModel?.currentVersionId
        ? await getVersion(activeModel.currentVersionId)
        : null;
      if (latestVersion?.deletedAt) latestVersion = activeVersions[0] || null;
      if (latestVersion && latestVersion.id !== context.loadedVersionId) {
        if (!await currentWorkDiffersFromLoaded()) {
          applySnapshot(latestVersion.snapshot);
          context.loadedVersionId = latestVersion.id;
          writeJson(CONTEXT_KEY, context);
          location.reload();
          return;
        }
        setSyncStatus(
          "Hay una versión más reciente en la nube; guárdala o ábrela desde el historial.",
          "error"
        );
      }
      await render();
    } catch (error) {
      console.error(error);
      setSyncStatus("Sin conexión · Reintentar", "error", true);
      if (!silent) setFeedback("No se pudo conectar con la nube.", "error");
    } finally {
      cloudSyncInProgress = false;
    }
  }

  function registerCloudProvider(provider) {
    cloudProvider = provider;
    setSyncStatus(
      provider.isSignedIn()
        ? "En la nube"
        : "Conectar con Google",
      provider.isSignedIn() ? "ready" : "",
      !provider.isSignedIn()
    );
  }

  window.RedVerdeVersions = {
    registerCloudProvider,
    synchronizeCloud,
    getWorkspaceData,
    mergeWorkspace,
    getContext: () => clone(context)
  };
})();
