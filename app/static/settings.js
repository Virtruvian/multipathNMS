const form = document.getElementById('target-form');
const message = document.getElementById('form-message');

form?.addEventListener('submit', async e => {
  e.preventDefault();
  message.textContent = 'Saving…';
  const response = await fetch('/api/targets', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name: document.getElementById('target-name').value.trim(),
      address: document.getElementById('target-address').value.trim(),
    }),
  });

  if (response.ok) {
    location.reload();
    return;
  }

  const data = await response.json().catch(() => ({}));
  message.textContent = data.detail || 'Unable to save target';
});

document.querySelectorAll('.delete-target').forEach(button => {
  button.addEventListener('click', async () => {
    if (!confirm('Delete this target and its samples?')) return;
    const response = await fetch(`/api/targets/${button.dataset.id}`, {method: 'DELETE'});
    if (response.ok) location.reload();
  });
});
