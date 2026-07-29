const firebaseConfig = window.RED_VERDE_FIREBASE_CONFIG;

if (firebaseConfig?.apiKey && firebaseConfig?.projectId && window.RedVerdeVersions) {
  const SDK_VERSION = "12.16.0";
  const baseUrl = `https://www.gstatic.com/firebasejs/${SDK_VERSION}`;
  const [
    { initializeApp },
    {
      browserLocalPersistence,
      getAuth,
      GoogleAuthProvider,
      onAuthStateChanged,
      setPersistence,
      signInWithPopup
    },
    {
      collection,
      doc,
      getDocs,
      getFirestore,
      setDoc
    }
  ] = await Promise.all([
    import(`${baseUrl}/firebase-app.js`),
    import(`${baseUrl}/firebase-auth.js`),
    import(`${baseUrl}/firebase-firestore.js`)
  ]);

  const app = initializeApp(firebaseConfig);
  const auth = getAuth(app);
  const firestore = getFirestore(app);
  const googleProvider = new GoogleAuthProvider();

  await setPersistence(auth, browserLocalPersistence);

  function requireUser() {
    if (!auth.currentUser) throw new Error("Debes iniciar sesión.");
    return auth.currentUser;
  }

  function modelsCollection(userId) {
    return collection(firestore, "users", userId, "models");
  }

  function modelDocument(userId, modelId) {
    return doc(firestore, "users", userId, "models", modelId);
  }

  function versionsCollection(userId, modelId) {
    return collection(firestore, "users", userId, "models", modelId, "versions");
  }

  function versionDocument(userId, modelId, versionId) {
    return doc(firestore, "users", userId, "models", modelId, "versions", versionId);
  }

  async function pushModelAndVersion(model, version) {
    const user = requireUser();
    await setDoc(modelDocument(user.uid, model.id), model);
    await setDoc(versionDocument(user.uid, model.id, version.id), version);
  }

  const cloudProvider = {
    isSignedIn() {
      return Boolean(auth.currentUser);
    },

    getUserLabel() {
      const user = auth.currentUser;
      return user?.email || user?.displayName || "usuario";
    },

    async signIn() {
      if (auth.currentUser) return auth.currentUser;
      const result = await signInWithPopup(auth, googleProvider);
      return result.user;
    },

    async pushVersion(model, version) {
      await pushModelAndVersion(model, version);
    },

    async pullWorkspace() {
      const user = requireUser();
      const modelsSnapshot = await getDocs(modelsCollection(user.uid));
      const models = modelsSnapshot.docs.map(item => item.data());
      const versions = [];

      for (const model of models) {
        const snapshot = await getDocs(versionsCollection(user.uid, model.id));
        snapshot.docs.forEach(item => versions.push(item.data()));
      }

      return { models, versions };
    },

    async pushWorkspace(workspace) {
      requireUser();
      for (const model of workspace.models) {
        await setDoc(modelDocument(auth.currentUser.uid, model.id), model);
      }
      for (const version of workspace.versions) {
        await setDoc(
          versionDocument(auth.currentUser.uid, version.modelId, version.id),
          version
        );
      }
    }
  };

  onAuthStateChanged(auth, user => {
    window.RedVerdeVersions.registerCloudProvider(cloudProvider);
    if (user) window.RedVerdeVersions.synchronizeCloud(true);
  });
}
