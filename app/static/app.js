(() => {
  const state = document.getElementById('ws-state');
  let retry;

  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${scheme}://${location.host}/ws/live`);
    window.multipathSocket = socket;

    socket.onopen = () => {
      if (state) {
        state.textContent = 'live';
        state.classList.add('connected');
      }
      socket.send('hello');
    };

    socket.onmessage = event => {
      try {
        const payload = JSON.parse(event.data);
        window.dispatchEvent(new CustomEvent('multipath-live', {detail: payload}));
      } catch (_) {}
    };

    socket.onclose = () => {
      if (state) {
        state.textContent = 'reconnecting';
        state.classList.remove('connected');
      }
      clearTimeout(retry);
      retry = setTimeout(connect, 2000);
    };
  }

  connect();
})();
