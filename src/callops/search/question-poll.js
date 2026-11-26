const form = document.querySelector('form[action="/questions"]');
let edited = false;
form.addEventListener('input', () => { edited = true; });

window.setTimeout(() => {
    if (!edited && !form.matches(':focus-within')) {
        window.location.reload();
    }
}, 3000);
