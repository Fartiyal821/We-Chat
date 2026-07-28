
    // Replace the values below with your own Firebase project config.
    window.firebaseConfig = {
      apiKey: "YOUR_API_KEY",
      authDomain: "YOUR_PROJECT_ID.firebaseapp.com",
      projectId: "YOUR_PROJECT_ID",
      appId: "YOUR_APP_ID",
    };

    window.firebaseConfigured = window.firebaseConfig.apiKey !== "YOUR_API_KEY" &&
      window.firebaseConfig.authDomain !== "YOUR_PROJECT_ID.firebaseapp.com" &&
      window.firebaseConfig.projectId !== "YOUR_PROJECT_ID" &&
      window.firebaseConfig.appId !== "YOUR_APP_ID";

    if (window.firebaseConfigured && typeof firebase !== 'undefined') {
      firebase.initializeApp(window.firebaseConfig);
    }
  