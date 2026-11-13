const dueFields = document.querySelector("#new-due-fields[data-collapsible]");
if (dueFields) {
  const toggle = document.querySelector("#toggle-due");
  dueFields.hidden = true;
  toggle.hidden = false;
  toggle.setAttribute("aria-expanded", "false");
  toggle.addEventListener("click", () => {
    const collapsed = !dueFields.hidden;
    dueFields.hidden = collapsed;
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.textContent = collapsed ? "기한 설정" : "기한 접기";
    if (!collapsed) dueFields.querySelector("input").focus();
  });
}

const clearDue = document.querySelector("[data-clear-due]");
if (clearDue) {
  const due = document.getElementById(clearDue.dataset.clearDue);
  const update = () => { clearDue.hidden = !due.value; };
  due.addEventListener("input", update);
  clearDue.addEventListener("click", () => {
    due.value = "";
    due.focus();
    update();
  });
  update();
}
