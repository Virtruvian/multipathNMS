(() => {
  function errorText(data) {
    return Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('; ') : data.detail || 'Unable to save user';
  }

  async function send(path, method, body) {
    const response = await fetch(path, {method, headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorText(data));
    return data;
  }

  const form = document.getElementById('user-form');
  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type=submit]');
    const message = document.getElementById('user-form-message');
    button.disabled = true;
    message.textContent = 'Saving…';
    try {
      await send('/api/users', 'POST', {
        username: document.getElementById('user-name').value.trim(),
        password: document.getElementById('user-password').value,
        role: document.getElementById('user-role').value,
      });
      document.getElementById('user-password').value = '';
      location.reload();
    } catch (error) {message.textContent = error.message || 'Unable to reach the server';}
    finally {button.disabled = false;}
  });

  document.querySelectorAll('.user-settings').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button[type=submit]');
      const note = form.querySelector('.settings-message');
      const values = {role: form.elements.role.value};
      if (form.elements.password.value) values.password = form.elements.password.value;
      button.disabled = true;
      note.textContent = 'Saving…';
      try {
        await send('/api/users/' + form.dataset.id, 'PATCH', values);
        form.elements.password.value = '';
        location.reload();
      } catch (error) {note.textContent = error.message || 'Unable to reach the server';}
      finally {button.disabled = false;}
    });
  });

  document.querySelectorAll('.delete-user').forEach(button => {
    button.addEventListener('click', async () => {
      if (!confirm('Delete user ' + button.dataset.username + '? Access will be revoked.')) return;
      const note = button.closest('.user-row').querySelector('.settings-message');
      button.disabled = true;
      try {
        await send('/api/users/' + button.dataset.id, 'DELETE');
        location.reload();
      } catch (error) {
        note.textContent = error.message || 'Unable to reach the server';
        button.disabled = false;
      }
    });
  });
})();
