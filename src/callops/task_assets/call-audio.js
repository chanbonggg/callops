const start = document.querySelector(".audio-start");
const audio = start.nextElementSibling;
const message = audio.nextElementSibling;

audio.hidden = true;
start.hidden = false;
start.addEventListener("click", () => {
  start.hidden = true;
  audio.hidden = false;
  audio.focus();
  audio.play().catch(() => { message.hidden = false; });
});
audio.addEventListener("playing", () => { message.hidden = true; });
