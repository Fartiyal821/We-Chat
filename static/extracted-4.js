
    /*
      Firebase Google Login Integration Example:
      1) Create a Firebase project at https://console.firebase.google.com
      2) Enable Google sign-in under Authentication > Sign-in method.
      3) Add Firebase SDK script tags above this script block.
      4) Initialize Firebase with your app config before the chat logic runs.

      Example config:
        const firebaseConfig = {
          apiKey: "YOUR_API_KEY",
          authDomain: "YOUR_PROJECT_ID.firebaseapp.com",
          projectId: "YOUR_PROJECT_ID",
          appId: "YOUR_APP_ID",
        };
        firebase.initializeApp(firebaseConfig);
    */

    const wsScheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${wsScheme}://${window.location.host}/ws`;
    const authOverlay = document.getElementById('authOverlay');
    const statusText = document.getElementById('statusText');
    const chatWindow = document.getElementById('chatWindow');
    const inputForm = document.getElementById('inputForm');
    const messageInput = document.getElementById('messageInput');
    const displayNameInput = document.getElementById('displayName');
    const loginButton = document.getElementById('loginButton');
    const googleLoginButton = document.getElementById('googleLoginButton');

    if (!window.firebaseConfigured || typeof firebase === 'undefined') {
      googleLoginButton.disabled = true;
      googleLoginButton.title = 'Google login is not configured. Set up Firebase config in static/index.html.';
      googleLoginButton.style.opacity = '0.65';
      googleLoginButton.style.cursor = 'not-allowed';
    }

    let websocket;
    let currentUser = '';

    const savedName = localStorage.getItem('wechatDisplayName');
    if (savedName) {
      currentUser = savedName;
      hideAuth();
      connectSocket();
    }

    function showAuth() {
      authOverlay.style.display = 'grid';
      document.body.style.overflow = 'hidden';
      displayNameInput.focus();
    }

    function hideAuth() {
      authOverlay.style.display = 'none';
      document.body.style.overflow = '';
    }

    function formatTime(iso) {
      const date = new Date(iso);
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function appendMessage({ username, content, timestamp }, isMine) {
      const row = document.createElement('div');
      row.className = `message-row ${isMine ? 'mine' : ''}`;

      const bubble = document.createElement('article');
      bubble.className = `bubble ${isMine ? 'mine' : ''}`;
      bubble.innerHTML = `
        <div class="bubble-title">
          <span>${username}</span>
          <span>${formatTime(timestamp)}</span>
        </div>
        <div class="bubble-text"></div>
      `;
      bubble.querySelector('.bubble-text').textContent = content;
      row.appendChild(bubble);
      chatWindow.appendChild(row);
      chatWindow.scrollTop = chatWindow.scrollHeight;
    }

    function setStatus(text) {
      statusText.textContent = text;
    }

    async function connectSocket() {
      websocket = new WebSocket(wsUrl);
      setStatus('Connecting to server...');

      websocket.addEventListener('open', () => {
        setStatus('Connected');
      });

      websocket.addEventListener('message', (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === 'history' && Array.isArray(payload.messages)) {
            payload.messages.forEach((msg) => appendMessage(msg, msg.username === currentUser));
            return;
          }
          if (payload.type === 'message') {
            appendMessage(payload, payload.username === currentUser);
          }
        } catch (error) {
          console.warn('Malformed message from server', error);
        }
      });

      websocket.addEventListener('close', () => {
        setStatus('Disconnected. Reconnecting in 3s...');
        setTimeout(connectSocket, 3000);
      });

      websocket.addEventListener('error', () => {
        setStatus('Connection error. Check server.');
      });
    }

    function sendMessage(content) {
      if (!websocket || websocket.readyState !== WebSocket.OPEN) {
        return;
      }
      const payload = {
        type: 'message',
        username: currentUser,
        content,
      };
      websocket.send(JSON.stringify(payload));
    }

    inputForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const content = messageInput.value.trim();
      if (!content) {
        return;
      }
      sendMessage(content);
      messageInput.value = '';
    });

    loginButton.addEventListener('click', () => {
      const value = displayNameInput.value.trim();
      if (!value) {
        displayNameInput.focus();
        return;
      }
      currentUser = value;
      localStorage.setItem('wechatDisplayName', currentUser);
      hideAuth();
      connectSocket();
    });

    googleLoginButton.addEventListener('click', async () => {
      if (!window.firebaseConfigured || typeof firebase === 'undefined') {
        alert('Google login is not configured. Open static/index.html and replace the placeholder Firebase config values with your project settings.');
        return;
      }

      try {
        const provider = new firebase.auth.GoogleAuthProvider();
        const result = await firebase.auth().signInWithPopup(provider);
        const user = result.user;
        if (user && user.displayName) {
          currentUser = user.displayName;
          localStorage.setItem('wechatDisplayName', currentUser);
          hideAuth();
          connectSocket();
        }
      } catch (error) {
        console.error('Google login failed', error);
        alert('Google login failed. Please try again or use guest login.');
      }
    });

    if (!savedName) {
      showAuth();
    }
  