(function () {
  "use strict";

  const DB_NAME = "redVerdeWorkspace";
  const DB_VERSION = 1;
  const MODELS_STORE = "models";
  const VERSIONS_STORE = "versions";
  const CONTEXT_KEY = "redVerde_workspace_context_v1";
  const BOOTSTRAP_KEY = "redVerde_active_version_bootstrap_v1";
  const SNAPSHOT_SCHEMA_VERSION = 1;
  const EXPORT_FORMAT = "red-verde-workspace";
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
    return versions.sort((a, b) => Number(b.versionNumber) - Number(a.versionNumber));
  }

  async function putModel(model) {
    const transaction = database.transaction(MODELS_STORE, "readwrite");
    transaction.objectStore(MODELS_STORE).put(model);
    await transactionDone(transaction);
  }

  function captureSnapshot() {
    const network = {
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
      cutoffDate: today(),
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
      throw new Error("No se encontró el modelo activo.");
    }

    const now = new Date().toISOString();
    const version = {
      id: makeId("version"),
      modelId,
      versionNumber: Number(model.nextVersionNumber) || 1,
      savedAt: now,
      cutoffDate: metadata.cutoffDate || today(),
      note: String(metadata.note || "").trim(),
      parentVersionId: context.loadedVersionId || model.currentVersionId || null,
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

  async function initialize() {
    try {
      database = await openDatabase();
      let models = await getAllModels();

      if (!models.length) {
        const initial = await createModel("Modelo de prueba", captureSnapshot(),
          "Migración inicial del modelo existente");
        models = [initial.model];
        context = {
          activeModelId: initial.model.id,
          loadedVersionId: initial.version.id
        };
        writeJson(CONTEXT_KEY, context);
      }

      activeModel = models.find(model => model.id === context.activeModelId) || models[0];
      let loadedVersion = context.loadedVersionId
        ? await getVersion(context.loadedVersionId)
        : null;
      if (!loadedVersion || loadedVersion.modelId !== activeModel.id) {
        loadedVersion = await getVersion(activeModel.currentVersionId);
      }
      context = {
        activeModelId: activeModel.id,
        loadedVersionId: loadedVersion?.id || null
      };
      writeJson(CONTEXT_KEY, context);
      activeVersions = await getVersions(activeModel.id);
      updateModelTitle();
      await render();
      setFeedback("Biblioteca local preparada.", "success");
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
    toggle.setAttribute("aria-label", "Abrir modelos y versiones");
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
          <h2 class="version-library-title">Modelos y versiones</h2>
        </div>
        <button class="version-library-close" type="button" aria-label="Cerrar">×</button>
      </header>
      <div class="version-library-content">
        <section class="version-card">
          <h3 class="version-card-title">Modelo activo</h3>
          <div class="version-field">
            <label for="versionModelSelect">Modelo</label>
            <select id="versionModelSelect"></select>
          </div>
          <div class="version-secondary-actions">
            <button id="duplicateModelButton" type="button">Duplicar modelo</button>
            <button id="renameModelButton" type="button">Renombrar</button>
          </div>
        </section>

        <section class="version-card">
          <h3 class="version-card-title">Guardar una versión nueva</h3>
          <p class="version-current">Versión abierta: <strong id="loadedVersionLabel">—</strong></p>
          <div class="version-field">
            <label for="versionCutoffDate">Fecha de corte</label>
            <input id="versionCutoffDate" type="date" />
          </div>
          <div class="version-field">
            <label for="versionNote">Descripción opcional</label>
            <textarea id="versionNote" maxlength="300" placeholder="Ej.: Actualización después de inspección"></textarea>
          </div>
          <button id="saveNewVersionButton" class="version-primary-action" type="button">
            Guardar nueva versión
          </button>
          <p id="versionFeedback" class="version-feedback" aria-live="polite"></p>
          <p id="versionSyncStatus" class="version-sync-status">
            <span class="version-sync-dot"></span>
            <span>Guardado local activo; Firebase pendiente de configurar.</span>
          </p>
          <div class="version-secondary-actions">
            <button id="syncCloudButton" type="button">Sincronizar</button>
            <button id="exportWorkspaceButton" type="button">Exportar copia</button>
          </div>
          <div class="version-secondary-actions">
            <label class="version-import-label">
              Importar copia
              <input id="importWorkspaceInput" type="file" accept="application/json,.json" />
            </label>
          </div>
        </section>

        <section class="version-card">
          <h3 class="version-card-title">Historial</h3>
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
      duplicateButton: panel.querySelector("#duplicateModelButton"),
      renameButton: panel.querySelector("#renameModelButton"),
      cutoffDate: panel.querySelector("#versionCutoffDate"),
      note: panel.querySelector("#versionNote"),
      saveButton: panel.querySelector("#saveNewVersionButton"),
      feedback: panel.querySelector("#versionFeedback"),
      syncStatus: panel.querySelector("#versionSyncStatus"),
      syncButton: panel.querySelector("#syncCloudButton"),
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
    ui.duplicateButton.addEventListener("click", duplicateActiveModel);
    ui.renameButton.addEventListener("click", renameActiveModel);
    ui.exportButton.addEventListener("click", exportWorkspace);
    ui.importInput.addEventListener("change", importWorkspace);
    ui.syncButton.addEventListener("click", () => synchronizeCloud(false));
    ui.modelSelect.addEventListener("change", () => switchModel(ui.modelSelect.value));
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && ui.panel.classList.contains("open")) {
        setPanelOpen(false);
      }
    });
    ui.cutoffDate.value = today();
  }

  function setPanelOpen(open) {
    ui.panel.classList.toggle("open", open);
    ui.backdrop.classList.toggle("open", open);
    ui.panel.setAttribute("aria-hidden", open ? "false" : "true");
    ui.toggle.setAttribute("aria-expanded", open ? "true" : "false");
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

  function setFeedback(message = "", type = "") {
    ui.feedback.textContent = message;
    ui.feedback.className = `version-feedback${type ? ` ${type}` : ""}`;
  }

  function setSyncStatus(message, type = "") {
    ui.syncStatus.className = `version-sync-status${type ? ` ${type}` : ""}`;
    ui.syncStatus.lastElementChild.textContent = message;
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

    const loaded = activeVersions.find(version => version.id === context.loadedVersionId);
    ui.loadedVersionLabel.textContent = loaded ? `v${String(loaded.versionNumber).padStart(3, "0")}` : "—";
    ui.versionsList.innerHTML = "";

    if (!activeVersions.length) {
      ui.versionsList.innerHTML = '<p class="version-list-empty">Todavía no hay versiones.</p>';
      return;
    }

    activeVersions.forEach(version => {
      const item = document.createElement("article");
      const isLoaded = version.id === context.loadedVersionId;
      item.className = `version-item${isLoaded ? " active" : ""}`;
      const content = document.createElement("div");
      const title = document.createElement("p");
      const meta = document.createElement("p");
      title.className = "version-item-title";
      meta.className = "version-item-meta";
      title.textContent = `v${String(version.versionNumber).padStart(3, "0")} · corte ${version.cutoffDate || "sin fecha"}`;
      meta.textContent = formatDateTime(version.savedAt);
      content.append(title, meta);
      if (version.note) {
        const note = document.createElement("p");
        note.className = "version-item-note";
        note.textContent = version.note;
        content.appendChild(note);
      }
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = isLoaded ? "Abierta" : "Abrir";
      button.disabled = isLoaded;
      button.addEventListener("click", () => openVersion(version.id));
      item.append(content, button);
      ui.versionsList.appendChild(item);
    });
  }

  async function saveNewVersion() {
    if (!activeModel || ui.saveButton.disabled) return;
    ui.saveButton.disabled = true;
    setFeedback("Guardando…");
    try {
      const result = await createVersion(activeModel.id, captureSnapshot(), {
        cutoffDate: ui.cutoffDate.value,
        note: ui.note.value
      });
      activeModel = result.model;
      context.loadedVersionId = result.version.id;
      writeJson(CONTEXT_KEY, context);
      activeVersions = await getVersions(activeModel.id);
      ui.note.value = "";
      await render();
      setFeedback(
        `Versión v${String(result.version.versionNumber).padStart(3, "0")} guardada.`,
        "success"
      );
      if (cloudProvider?.isSignedIn?.()) {
        try {
          await cloudProvider.pushVersion(clone(activeModel), clone(result.version));
          setSyncStatus("Versión guardada también en la nube.", "ready");
        } catch (cloudError) {
          console.error(cloudError);
          setSyncStatus(
            "La versión quedó guardada localmente y está pendiente de subir.",
            "error"
          );
        }
      } else {
        setSyncStatus("Versión guardada localmente; nube pendiente.", "");
      }
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo guardar la nueva versión.", "error");
    } finally {
      ui.saveButton.disabled = false;
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
      if (!version) throw new Error("Versión no encontrada.");
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

  async function switchModel(modelId) {
    if (!modelId || modelId === activeModel?.id) return;
    if (!await confirmDiscardingUnsavedWork()) {
      ui.modelSelect.value = activeModel.id;
      return;
    }
    const model = await getModel(modelId);
    const version = model ? await getVersion(model.currentVersionId) : null;
    if (!model || !version) {
      setFeedback("El modelo no tiene una versión válida.", "error");
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
    const suggested = `${activeModel?.name || "Modelo"} - copia`;
    const name = prompt("Nombre del nuevo modelo:", suggested)?.trim();
    if (!name) return;
    try {
      const result = await createModel(name, captureSnapshot(), "Modelo duplicado");
      applySnapshot(result.version.snapshot);
      writeJson(CONTEXT_KEY, {
        activeModelId: result.model.id,
        loadedVersionId: result.version.id
      });
      location.reload();
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo duplicar el modelo.", "error");
    }
  }

  async function renameActiveModel() {
    const name = prompt("Nuevo nombre del modelo:", activeModel?.name || "")?.trim();
    if (!name || name === activeModel?.name) return;
    try {
      activeModel.name = name;
      activeModel.updatedAt = new Date().toISOString();
      await putModel(activeModel);
      updateModelTitle();
      await render();
      setFeedback("Modelo renombrado.", "success");
    } catch (error) {
      console.error(error);
      setFeedback("No se pudo renombrar el modelo.", "error");
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
        item.id === importedContext.loadedVersionId && item.modelId === model.id) ||
        data.versions
          .filter(item => item.modelId === model.id)
          .sort((a, b) => Number(b.versionNumber) - Number(a.versionNumber))[0];
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
    const localVersionIds = new Set(local.versions.map(version => version.id));
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
      if (!localVersionIds.has(version.id)) versionsStore.add(version);
    });
    await transactionDone(transaction);
  }

  async function synchronizeCloud(silent = false) {
    if (cloudSyncInProgress) return;
    if (!cloudProvider) {
      setSyncStatus("Firebase aún no está configurado.", "error");
      if (!silent) setFeedback("Falta conectar el proyecto gratuito de Firebase.", "error");
      return;
    }
    cloudSyncInProgress = true;
    ui.syncButton.disabled = true;
    setSyncStatus("Sincronizando…");
    try {
      if (!cloudProvider.isSignedIn()) await cloudProvider.signIn();
      const remote = await cloudProvider.pullWorkspace();
      await mergeWorkspace(remote);
      const merged = await getWorkspaceData();
      await cloudProvider.pushWorkspace(merged);
      setSyncStatus(`Sincronizado como ${cloudProvider.getUserLabel()}.`, "ready");
      if (!silent) setFeedback("Biblioteca sincronizada.", "success");
      activeModel = await getModel(context.activeModelId);
      activeVersions = await getVersions(context.activeModelId);
      const latestVersion = activeModel?.currentVersionId
        ? await getVersion(activeModel.currentVersionId)
        : null;
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
      setSyncStatus("No se pudo completar la sincronización.", "error");
      setFeedback("Revisa la conexión o el inicio de sesión.", "error");
    } finally {
      cloudSyncInProgress = false;
      ui.syncButton.disabled = false;
    }
  }

  function registerCloudProvider(provider) {
    cloudProvider = provider;
    setSyncStatus(
      provider.isSignedIn()
        ? `Conectado como ${provider.getUserLabel()}.`
        : "Firebase preparado; inicia sesión para sincronizar.",
      provider.isSignedIn() ? "ready" : ""
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
