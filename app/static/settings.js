const form = document.getElementById('target-form');
const message = document.getElementById('form-message');
function errorText(data) {
  return Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('; ') : data.detail || 'Unable to save target';
}
const httpsToggle = document.getElementById('target-https');
httpsToggle?.addEventListener('change', () => {document.getElementById('target-https-path').disabled = !httpsToggle.checked;});

form?.addEventListener('submit', async e => {
  e.preventDefault();
  message.textContent = 'Saving…';
  const response = await fetch('/api/targets', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name: document.getElementById('target-name').value.trim(),
      address: document.getElementById('target-address').value.trim(),
      tcp_port: Number(document.getElementById('target-port').value),
      tcp_check_enabled: document.getElementById('target-tcp-check').checked,
      https_enabled: httpsToggle.checked,
      https_path: document.getElementById('target-https-path').value || '/',
    }),
  });

  if (response.ok) {
    location.reload();
    return;
  }

  const data = await response.json().catch(() => ({}));
  message.textContent = errorText(data);
});

document.querySelectorAll('.service-settings').forEach(settings => {
  const toggle = settings.elements.https_enabled;
  toggle.addEventListener('change', () => {settings.elements.https_path.disabled = !toggle.checked;});
  settings.addEventListener('submit', async event => {
    event.preventDefault();
    const button = settings.querySelector('button[type=submit]');
    const note = settings.querySelector('.settings-message');
    button.disabled = true;
    note.textContent = 'Saving…';
    try {
      const response = await fetch('/api/targets/' + settings.dataset.id, {method: 'PATCH',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
          enabled: settings.elements.enabled.checked, tcp_port: Number(settings.elements.tcp_port.value),
          tcp_check_enabled: settings.elements.tcp_check_enabled.checked,
          https_enabled: toggle.checked, https_path: settings.elements.https_path.value || '/'
        })});
      const data = await response.json();
      if (!response.ok) throw new Error(errorText(data));
      note.textContent = 'Saved. New settings apply on the next service round.';
    } catch (error) {note.textContent = String(error);}
    finally {button.disabled = false;}
  });
});

document.querySelectorAll('.delete-target').forEach(button => {
  button.addEventListener('click', async () => {
    if (!confirm('Delete this target and its samples?')) return;
    const response = await fetch(`/api/targets/${button.dataset.id}`, {method: 'DELETE'});
    if (response.ok) location.reload();
  });
});
