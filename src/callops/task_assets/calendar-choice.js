const form = document.getElementById('calendar-resolution');
const apply = form.querySelector('[data-apply]');

function updateApply() {
  apply.disabled = !form.querySelector('input[name="choice"]:checked');
}

form.addEventListener('change', updateApply);
window.addEventListener('pageshow', updateApply);
updateApply();
